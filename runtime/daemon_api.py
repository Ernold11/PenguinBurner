from __future__ import annotations

import base64
import importlib.metadata
import json
import os
from pathlib import Path
import signal
import socket
import socketserver
import select
import subprocess
import struct
import sys
import threading
import time
from typing import Any


DEFAULT_DAEMON_SOCKET = "/run/penguin-burnerd.sock"
AUTOSTART_ARGV_B64_ENV = "PENGUIN_BURNER_DAEMON_AUTOSTART_ARGV_B64"
AUTOSTART_PROGRAM_FILE_ENV = "PENGUIN_BURNER_DAEMON_PROGRAM_FILE"
ALLOWED_UID_ENV = "PENGUIN_BURNER_DAEMON_ALLOWED_UID"

# Persisted "last runtime action" so a daemon restart / reboot re-runs exactly
# what the user last applied -- including "reset to stock". The root daemon owns
# this file and prefers it over the systemd-unit env autostart, so the last user
# action always wins, with NO elevation prompt. install/uninstall-systemd clear
# it so an explicit "persist THIS profile" re-seeds from the unit instead.
LAST_RUNTIME_STATE_PATH = Path("/var/lib/penguin-burner/last-runtime.json")

_AUTOSTART_PROCESS: subprocess.Popen | None = None
_AUTOSTART_ARGV: list[str] = []
_ACTIVE_SCAN_PROCESS: subprocess.Popen | None = None
_ACTIVE_SCAN_ARGV: list[str] = []
_ACTIVE_SCAN_LOCK = threading.Lock()

# Per-game runtime profiles (Steam tab): the wrapper asks us to apply a
# profile and watch the game session's PID; when the last watched game exits
# we restore the persisted standing action. Watches survive user profile
# switches mid-game (last-writer-wins while running, default on last exit).
_GAME_WATCH_LOCK = threading.Lock()
_GAME_WATCHED_PIDS: dict[int, str] = {}
_GAME_RUNTIME_ACTIVE = False
# Whether a runtime child was running when the first watched game started —
# restore-on-exit must not resurrect a profile the user had stopped.
_GAME_PRIOR_RUNTIME_RUNNING = False
# Short debounce so a launcher chain that re-launches immediately (new wrapper
# call, new PID) does not thrash the profile between its processes.
_GAME_RESTORE_GRACE_S = 3.0
_RUNTIME_PROFILE_OPTION_FLAGS = {
    "--auto-uv-profile",
    "--silent-fan-curve",
    "--adaptive-auto-uv",
    "--gpu-index",
}
_RUNTIME_PROFILE_VALUE_FLAGS = {
    "--auto-uv-profile",
    "--gpu-index",
}

_AUTO_UV_OPTION_FLAGS = {
    "gpu_index": "--gpu-index",
    "auto_uv_mode": "--auto-uv-mode",
    "auto_uv_min_voltage_mv": "--auto-uv-min-voltage-mv",
    "auto_uv_max_clock_drop_pct": "--auto-uv-max-clock-drop-pct",
    "auto_uv_memory_offset_mhz": "--auto-uv-memory-offset-mhz",
    "auto_uv_power_limit_w": "--auto-uv-power-limit-w",
    "auto_uv_tail_rise_bins": "--auto-uv-tail-rise-bins",
    "auto_oc_target_voltage_mv": "--auto-oc-target-voltage-mv",
    "auto_oc_target_clock_mhz": "--auto-oc-target-clock-mhz",
}


def application_version() -> str:
    try:
        return importlib.metadata.version("penguin-burner")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.dev0"


def status_payload() -> dict[str, Any]:
    active_job = None
    state = "idle"
    if _ACTIVE_SCAN_PROCESS is not None:
        returncode = _ACTIVE_SCAN_PROCESS.poll()
        active_job = {
            "type": "auto_uv_scan",
            "argv": list(_ACTIVE_SCAN_ARGV),
            "pid": _ACTIVE_SCAN_PROCESS.pid,
            "returncode": returncode,
        }
        state = "auto_uv_scan_running" if returncode is None else "auto_uv_scan_stopped"
    elif _AUTOSTART_PROCESS is not None:
        returncode = _AUTOSTART_PROCESS.poll()
        active_job = {
            "type": "runtime_profile",
            "argv": list(_AUTOSTART_ARGV),
            "pid": _AUTOSTART_PROCESS.pid,
            "returncode": returncode,
        }
        state = (
            "runtime_profile_running"
            if returncode is None
            else "runtime_profile_stopped"
        )
    payload: dict[str, Any] = {
        "state": state,
        "active_job": active_job,
        "version": application_version(),
    }
    with _GAME_WATCH_LOCK:
        if _GAME_WATCHED_PIDS:
            payload["game_runtime"] = {
                "active": _GAME_RUNTIME_ACTIVE,
                "watched": [
                    {"pid": pid, "app_id": app_id}
                    for pid, app_id in sorted(_GAME_WATCHED_PIDS.items())
                ],
            }
    return payload


def handle_request(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("request must be a JSON object")
    method = payload.get("method")
    allowed = {"method"}
    if method == "start_runtime_profile":
        allowed.add("argv")
    if method == "start_game_runtime_profile":
        allowed.update({"argv", "watch_pid", "app_id"})
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError(f"unknown request field: {', '.join(unknown)}")
    if method == "status":
        return status_payload()
    if method == "stop_auto_uv_scan":
        return stop_auto_uv_scan()
    if method == "start_runtime_profile":
        return start_runtime_profile(payload.get("argv"))
    if method == "start_game_runtime_profile":
        return start_game_runtime_profile(
            payload.get("argv"),
            payload.get("watch_pid"),
            payload.get("app_id"),
        )
    if method == "stop_runtime_profile":
        return stop_runtime_profile()
    if not isinstance(method, str) or not method:
        raise ValueError("request method is required")
    raise ValueError(f"unknown daemon method: {method}")


class _DaemonRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        if not _peer_uid_allowed(self.request):
            self.wfile.write(
                b'{"ok":false,"error":"daemon client uid is not allowed"}\n'
            )
            self.wfile.flush()
            return
        for raw_line in self.rfile:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                request = json.loads(line)
                if _is_start_auto_uv_scan_request(request):
                    self._handle_start_auto_uv_scan(request)
                    return
                response = {"ok": True, "result": handle_request(request)}
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception as exc:
                response = {"ok": False, "error": str(exc)}
            try:
                self.wfile.write(
                    (json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8")
                )
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

    def _handle_start_auto_uv_scan(self, request: dict[str, Any]) -> None:
        unknown = sorted(set(request) - {"method", "options"})
        if unknown:
            self._write_stream_payload(
                {
                    "ok": False,
                    "error": f"unknown request field: {', '.join(unknown)}",
                }
            )
            return
        for payload in stream_auto_uv_scan(request.get("options")):
            if not self._write_stream_payload(payload):
                return

    def _write_stream_payload(self, payload: dict[str, Any]) -> bool:
        try:
            self.wfile.write(
                (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
            )
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return False
        return True


class _UnixDaemonServer(socketserver.ThreadingUnixStreamServer):
    allow_reuse_address = True
    daemon_threads = True


def serve_daemon_api(socket_path: str | Path = DEFAULT_DAEMON_SOCKET) -> None:
    _start_autostart_runtime_if_configured()
    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not path.is_socket():
            raise RuntimeError(f"daemon socket path exists and is not a socket: {path}")
        path.unlink()
    server = _UnixDaemonServer(str(path), _DaemonRequestHandler)
    os.chmod(path, 0o666)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def stream_auto_uv_scan(options: object):
    global _ACTIVE_SCAN_PROCESS, _ACTIVE_SCAN_ARGV
    command = _auto_uv_scan_command(options)
    with _ACTIVE_SCAN_LOCK:
        if _scan_running():
            yield {"ok": False, "error": "Auto-UV scan is already running"}
            return
        try:
            _clear_stale_auto_uv_stop_request()
        except Exception as exc:
            yield {
                "ok": False,
                "error": f"failed to clear stale Auto-UV stop request: {exc}",
            }
            return
        _stop_autostart_runtime_for_scan()
        process = subprocess.Popen(
            command,
            cwd="/",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        _ACTIVE_SCAN_PROCESS = process
        _ACTIVE_SCAN_ARGV = command[2:]
    yield {"ok": True, "control": "started", "pid": process.pid}
    exit_code = 1
    completed = False
    try:
        assert process.stdout is not None
        for line in process.stdout:
            yield {"ok": True, "line": line}
        exit_code = int(process.wait())
        completed = True
        yield {"ok": True, "control": "finished", "exit_code": exit_code}
    finally:
        if completed or process.poll() is not None:
            _finish_auto_uv_scan(process)
        else:
            _request_auto_uv_scan_stop(process, abort_final_choice=True)
            _start_detached_scan_monitor(process, kill_after_s=30.0)


def stop_auto_uv_scan() -> dict[str, Any]:
    process = _ACTIVE_SCAN_PROCESS
    if process is None or process.poll() is not None:
        return {"stopped": False, "state": "idle"}
    _request_auto_uv_scan_stop(process, abort_final_choice=False)
    return {"stopped": True, "pid": process.pid}


def start_runtime_profile(argv: object) -> dict[str, Any]:
    global _AUTOSTART_PROCESS, _AUTOSTART_ARGV
    runtime_argv = _runtime_profile_argv(argv)
    with _ACTIVE_SCAN_LOCK:
        if _scan_running():
            raise RuntimeError("cannot start a runtime profile while Auto-UV scan is running")
        _stop_autostart_runtime_for_scan()
        process = _spawn_runtime_child(runtime_argv)
        _AUTOSTART_PROCESS = process
        _AUTOSTART_ARGV = runtime_argv
    # Remember this as the last user action so a restart / reboot re-runs it.
    _persist_last_runtime_argv(runtime_argv, _daemon_program_file())
    return {"started": True, "pid": process.pid, "argv": list(runtime_argv)}


def stop_runtime_profile() -> dict[str, Any]:
    process = _AUTOSTART_PROCESS
    if process is None or process.poll() is not None:
        return {"stopped": False, "state": "idle"}
    _stop_autostart_runtime_for_scan()
    return {"stopped": True, "pid": process.pid}


def start_game_runtime_profile(
    argv: object,
    watch_pid: object,
    app_id: object = "",
) -> dict[str, Any]:
    """Apply a per-game profile and restore the standing one when it exits.

    Unlike start_runtime_profile this does NOT persist to last-runtime.json:
    the persisted file keeps the user's standing action, which is exactly
    what gets re-applied when the last watched game exits.
    """
    global _AUTOSTART_PROCESS, _AUTOSTART_ARGV
    global _GAME_RUNTIME_ACTIVE, _GAME_PRIOR_RUNTIME_RUNNING
    runtime_argv = _runtime_profile_argv(argv)
    try:
        pid = int(watch_pid)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise ValueError("watch_pid must be an integer") from error
    if pid <= 1 or not Path(f"/proc/{pid}").is_dir():
        raise ValueError(f"watch_pid {pid} is not a running process")
    with _ACTIVE_SCAN_LOCK:
        if _scan_running():
            raise RuntimeError(
                "cannot start a game runtime profile while Auto-UV scan is running"
            )
        with _GAME_WATCH_LOCK:
            if not _GAME_RUNTIME_ACTIVE:
                _GAME_PRIOR_RUNTIME_RUNNING = (
                    _AUTOSTART_PROCESS is not None
                    and _AUTOSTART_PROCESS.poll() is None
                )
        _stop_autostart_runtime_for_scan()
        try:
            process = _spawn_runtime_child(runtime_argv)
        except OSError:
            # The standing runtime is already stopped; try to bring it back
            # before reporting failure, so a broken game apply does not leave
            # the GPU with no profile at all.
            _spawn_persisted_standing_runtime()
            raise
        _AUTOSTART_PROCESS = process
        _AUTOSTART_ARGV = runtime_argv
        # Register the watch inside the scan lock so a concurrent restore
        # (previous game's watcher) cannot miss it and kill this profile.
        with _GAME_WATCH_LOCK:
            fresh_watch = pid not in _GAME_WATCHED_PIDS
            _GAME_WATCHED_PIDS[pid] = str(app_id or "")
            _GAME_RUNTIME_ACTIVE = True
    if fresh_watch:
        threading.Thread(
            target=_watch_game_pid, args=(pid,), daemon=True
        ).start()
    return {
        "started": True,
        "pid": process.pid,
        "watching_pid": pid,
        "argv": list(runtime_argv),
    }


def _spawn_runtime_child(
    runtime_argv: list[str], program_file: str = ""
) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, program_file or _daemon_program_file(), *runtime_argv],
        cwd="/",
    )


def _spawn_persisted_standing_runtime() -> bool:
    """Best-effort relaunch of the persisted standing action (caller holds
    _ACTIVE_SCAN_LOCK with no runtime child running)."""
    global _AUTOSTART_PROCESS, _AUTOSTART_ARGV
    persisted = _load_last_runtime_argv()
    if persisted is None:
        return False
    argv, program_file = persisted
    try:
        process = _spawn_runtime_child(argv, program_file)
    except OSError:
        return False
    _AUTOSTART_PROCESS = process
    _AUTOSTART_ARGV = list(argv)
    return True


def _watch_game_pid(pid: int) -> None:
    global _GAME_RUNTIME_ACTIVE
    try:
        _wait_for_pid_exit(pid)
    finally:
        time.sleep(_GAME_RESTORE_GRACE_S)
        with _GAME_WATCH_LOCK:
            _GAME_WATCHED_PIDS.pop(pid, None)
            skip_restore = _GAME_WATCHED_PIDS or not _GAME_RUNTIME_ACTIVE
            if not skip_restore:
                _GAME_RUNTIME_ACTIVE = False
        if not skip_restore:
            _restore_standing_runtime()


def _wait_for_pid_exit(pid: int) -> None:
    try:
        fd = os.pidfd_open(pid)
    except OSError:
        fd = None
    if fd is not None:
        try:
            # poll(), not select(): select breaks on fd numbers >= 1024.
            poller = select.poll()
            poller.register(fd, select.POLLIN)
            poller.poll()
        finally:
            os.close(fd)
        return
    while Path(f"/proc/{pid}").is_dir():
        time.sleep(1.0)


def _restore_standing_runtime() -> None:
    """Bring back what ran before the first game: the persisted standing
    action if a runtime was active then, otherwise stay stopped."""
    with _ACTIVE_SCAN_LOCK:
        with _GAME_WATCH_LOCK:
            # A new game may have taken over between our watch-set check and
            # this lock acquisition; its profile must not be torn down.
            if _GAME_WATCHED_PIDS or _GAME_RUNTIME_ACTIVE:
                return
        if _scan_running():
            return
        _stop_autostart_runtime_for_scan()
        if not _GAME_PRIOR_RUNTIME_RUNNING:
            return
        _spawn_persisted_standing_runtime()


def _is_start_auto_uv_scan_request(request: object) -> bool:
    return isinstance(request, dict) and request.get("method") == "start_auto_uv_scan"


def _auto_uv_scan_command(options: object) -> list[str]:
    if not isinstance(options, dict):
        raise ValueError("start_auto_uv_scan options must be a JSON object")
    unknown = sorted(set(options) - set(_AUTO_UV_OPTION_FLAGS))
    if unknown:
        raise ValueError(f"unknown Auto-UV option: {', '.join(unknown)}")
    command = [
        sys.executable,
        _daemon_program_file(),
        "--auto-uv-voltage-scan",
        "--json-events",
        "--auto-uv-require-final-choice",
    ]
    for key, flag in _AUTO_UV_OPTION_FLAGS.items():
        value = options.get(key)
        if value in (None, ""):
            continue
        if not isinstance(value, (str, int, float, bool)):
            raise ValueError(f"Auto-UV option {key} must be scalar")
        command.extend([flag, _command_value_text(value)])
    return command


def _runtime_profile_argv(argv: object) -> list[str]:
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        raise ValueError("runtime profile argv must be a JSON string list")
    clean = [str(item) for item in argv]
    index = 0
    while index < len(clean):
        item = clean[index]
        if item in _RUNTIME_PROFILE_VALUE_FLAGS:
            if index + 1 >= len(clean):
                raise ValueError(f"{item} requires a value")
            index += 2
            continue
        if item.startswith("--auto-uv-profile=") or item.startswith("--gpu-index="):
            index += 1
            continue
        if item in _RUNTIME_PROFILE_OPTION_FLAGS:
            index += 1
            continue
        raise ValueError(f"unsupported runtime profile argument: {item}")
    return clean


def _daemon_program_file() -> str:
    configured = os.environ.get(AUTOSTART_PROGRAM_FILE_ENV, "").strip()
    if configured:
        return str(Path(configured).resolve())
    return str(Path(sys.argv[0]).resolve())


def _command_value_text(value: object) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _scan_running() -> bool:
    return _ACTIVE_SCAN_PROCESS is not None and _ACTIVE_SCAN_PROCESS.poll() is None


def _start_detached_scan_monitor(
    process: subprocess.Popen,
    *,
    kill_after_s: float | None = None,
) -> None:
    threading.Thread(
        target=_drain_detached_scan_output,
        args=(process,),
        daemon=True,
        name="penguin-burner-auto-uv-scan-drain",
    ).start()
    thread = threading.Thread(
        target=_wait_for_detached_scan,
        args=(process, kill_after_s),
        daemon=True,
        name="penguin-burner-auto-uv-scan-monitor",
    )
    thread.start()


def _wait_for_detached_scan(
    process: subprocess.Popen,
    kill_after_s: float | None,
) -> None:
    try:
        if kill_after_s is None:
            process.wait()
        else:
            try:
                process.wait(timeout=max(0.1, float(kill_after_s)))
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    finally:
        _finish_auto_uv_scan(process)


def _drain_detached_scan_output(process: subprocess.Popen) -> None:
    stdout = process.stdout
    if stdout is None:
        return
    try:
        for _line in stdout:
            pass
    except (OSError, ValueError):
        pass


def _finish_auto_uv_scan(process: subprocess.Popen) -> None:
    global _ACTIVE_SCAN_PROCESS, _ACTIVE_SCAN_ARGV
    restart_autostart = False
    with _ACTIVE_SCAN_LOCK:
        if _ACTIVE_SCAN_PROCESS is process:
            _ACTIVE_SCAN_PROCESS = None
            _ACTIVE_SCAN_ARGV = []
            restart_autostart = True
    if restart_autostart:
        _start_autostart_runtime_if_configured()


def _stop_autostart_runtime_for_scan() -> None:
    global _AUTOSTART_PROCESS
    process = _AUTOSTART_PROCESS
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)
    _AUTOSTART_PROCESS = None


def _request_auto_uv_scan_stop(
    process: subprocess.Popen,
    *,
    abort_final_choice: bool,
) -> None:
    _write_auto_uv_stop_request(abort_final_choice=abort_final_choice)
    try:
        process.send_signal(signal.SIGINT)
    except OSError:
        pass


def _write_auto_uv_stop_request(*, abort_final_choice: bool = False) -> None:
    try:
        from auto_uv.persistence.auto_uv_persisted_json_files import (
            auto_uv_stop_request_path,
        )

        path = auto_uv_stop_request_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        reason = "abort-final-choice" if abort_final_choice else "offer-final-choice"
        path.write_text(
            "stop requested by PenguinBurner daemon client\n"
            f"reason={reason}\n",
            encoding="utf-8",
        )
    except Exception:
        pass


def _clear_stale_auto_uv_stop_request() -> None:
    from auto_uv.persistence.auto_uv_persisted_json_files import (
        clear_auto_uv_stop_request,
    )

    clear_auto_uv_stop_request()


def _persist_last_runtime_argv(argv: list[str], program_file: str) -> None:
    """Record the last runtime action so a restart / reboot re-runs it."""
    try:
        LAST_RUNTIME_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        LAST_RUNTIME_STATE_PATH.write_text(
            json.dumps({"argv": list(argv), "program_file": str(program_file)}),
            encoding="utf-8",
        )
    except Exception:
        pass  # best-effort; falls back to the unit env autostart


def _load_last_runtime_argv() -> tuple[list[str], str] | None:
    try:
        data = json.loads(LAST_RUNTIME_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    argv = data.get("argv") if isinstance(data, dict) else None
    program_file = str(data.get("program_file") or "").strip() if isinstance(data, dict) else ""
    if (
        isinstance(argv, list)
        and all(isinstance(item, str) for item in argv)
        and program_file
    ):
        return list(argv), program_file
    return None


def clear_last_runtime_state() -> None:
    """Forget the persisted last action (used by install/uninstall-systemd)."""
    try:
        LAST_RUNTIME_STATE_PATH.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def _start_autostart_runtime_if_configured() -> None:
    global _AUTOSTART_PROCESS, _AUTOSTART_ARGV
    if _AUTOSTART_PROCESS is not None:
        return
    # Prefer the persisted last user action (incl. reset-to-stock) over the
    # unit's env autostart, so a reboot re-runs exactly what the user last did.
    persisted = _load_last_runtime_argv()
    if persisted is not None:
        argv, program_file = persisted
    else:
        encoded = os.environ.get(AUTOSTART_ARGV_B64_ENV, "").strip()
        program_file = os.environ.get(AUTOSTART_PROGRAM_FILE_ENV, "").strip()
        if not encoded or not program_file:
            return
        try:
            argv = json.loads(base64.b64decode(encoded).decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"failed to decode daemon autostart argv: {exc}") from exc
        if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
            raise RuntimeError("daemon autostart argv must be a JSON string list")
        # Seed the state file from the unit so subsequent boots are consistent.
        _persist_last_runtime_argv(argv, program_file)
    _AUTOSTART_ARGV = list(argv)
    _AUTOSTART_PROCESS = subprocess.Popen(
        [sys.executable, str(Path(program_file).resolve()), *_AUTOSTART_ARGV],
        cwd="/",
    )


def _peer_uid_allowed(connection: socket.socket) -> bool:
    allowed_uid = os.environ.get(ALLOWED_UID_ENV, "").strip()
    if not allowed_uid:
        return True
    try:
        uid = _peer_uid(connection)
    except OSError:
        return False
    return uid == 0 or str(uid) == allowed_uid


def _peer_uid(connection: socket.socket) -> int:
    credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    _pid, uid, _gid = struct.unpack("3i", credentials)
    return int(uid)
