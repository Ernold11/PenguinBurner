from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time

from common.subprocess_locale import stable_subprocess_env

from .assets import resolve_q2rtx_executable
from .models import Q2RTXStabilityConfig


def _wrap_command_for_live_output(command: list[str]) -> list[str]:
    stdbuf = shutil.which("stdbuf")
    if not stdbuf:
        return list(command)
    return [stdbuf, "-oL", "-eL", *command]


def _child_process_group_preexec(
    child_preexec_fn,
):
    def _preexec() -> None:
        os.setsid()
        if child_preexec_fn is not None:
            child_preexec_fn()

    return _preexec


def _terminate_process_group(
    process: subprocess.Popen | None,
    *,
    timeout_s: float = 5.0,
) -> None:
    if process is None:
        return
    try:
        if process.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=timeout_s)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except ProcessLookupError:
            return
        process.wait(timeout=timeout_s)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def _managed_q2rtx_process_groups(config: Q2RTXStabilityConfig) -> set[int]:
    try:
        executable_path, _workdir = resolve_q2rtx_executable()
    except Exception:
        return set()
    executable_text = str(executable_path)
    result = subprocess.run(
        ["ps", "-eo", "pid=,pgid=,args="],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=stable_subprocess_env(),
        check=False,
    )
    if result.returncode != 0:
        return set()
    current_pid = os.getpid()
    current_pgid = os.getpgid(current_pid)
    process_groups: set[int] = set()
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            pgid = int(parts[1])
        except ValueError:
            continue
        if pid == current_pid or pgid == current_pgid:
            continue
        args = parts[2]
        if executable_text in args:
            process_groups.add(pgid)
    return process_groups


def cleanup_managed_q2rtx_processes(
    config: Q2RTXStabilityConfig,
    *,
    log=None,
) -> int:
    process_groups = _managed_q2rtx_process_groups(config)
    if not process_groups:
        return 0
    stopped = 0
    for pgid in sorted(process_groups):
        try:
            os.killpg(int(pgid), signal.SIGTERM)
            stopped += 1
        except ProcessLookupError:
            continue
        except Exception as exc:
            if log is not None:
                log(f"Q2RTX cleanup: failed to terminate process group {pgid}: {exc}")
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        live_groups = []
        for pgid in sorted(process_groups):
            try:
                os.killpg(int(pgid), 0)
            except ProcessLookupError:
                continue
            except Exception:
                continue
            live_groups.append(pgid)
        if not live_groups:
            break
        time.sleep(0.1)
    for pgid in sorted(process_groups):
        try:
            os.killpg(int(pgid), 0)
        except ProcessLookupError:
            continue
        except Exception:
            continue
        try:
            os.killpg(int(pgid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception as exc:
            if log is not None:
                log(f"Q2RTX cleanup: failed to kill process group {pgid}: {exc}")
    if log is not None and stopped:
        log(f"Q2RTX cleanup: stopped {stopped} managed process group(s).")
    return stopped
