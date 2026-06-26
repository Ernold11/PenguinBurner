from __future__ import annotations

import base64
import importlib.metadata
import json
import os
from pathlib import Path
import socket
import socketserver
import subprocess
import struct
import sys
from typing import Any


DEFAULT_DAEMON_SOCKET = "/run/penguin-burnerd.sock"
AUTOSTART_ARGV_B64_ENV = "PENGUIN_BURNER_DAEMON_AUTOSTART_ARGV_B64"
AUTOSTART_PROGRAM_FILE_ENV = "PENGUIN_BURNER_DAEMON_PROGRAM_FILE"
ALLOWED_UID_ENV = "PENGUIN_BURNER_DAEMON_ALLOWED_UID"

_AUTOSTART_PROCESS: subprocess.Popen | None = None
_AUTOSTART_ARGV: list[str] = []


def application_version() -> str:
    try:
        return importlib.metadata.version("penguin-burner")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.dev0"


def status_payload() -> dict[str, Any]:
    active_job = None
    state = "idle"
    if _AUTOSTART_PROCESS is not None:
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
    return {
        "state": state,
        "active_job": active_job,
        "version": application_version(),
    }


def handle_request(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("request must be a JSON object")
    allowed = {"method"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError(f"unknown request field: {', '.join(unknown)}")
    method = payload.get("method")
    if method == "status":
        return status_payload()
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
                response = {"ok": True, "result": handle_request(request)}
            except Exception as exc:
                response = {"ok": False, "error": str(exc)}
            self.wfile.write(
                (json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8")
            )
            self.wfile.flush()


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


def _start_autostart_runtime_if_configured() -> None:
    global _AUTOSTART_PROCESS, _AUTOSTART_ARGV
    encoded = os.environ.get(AUTOSTART_ARGV_B64_ENV, "").strip()
    program_file = os.environ.get(AUTOSTART_PROGRAM_FILE_ENV, "").strip()
    if not encoded or not program_file or _AUTOSTART_PROCESS is not None:
        return
    try:
        argv = json.loads(base64.b64decode(encoded).decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"failed to decode daemon autostart argv: {exc}") from exc
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        raise RuntimeError("daemon autostart argv must be a JSON string list")
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
