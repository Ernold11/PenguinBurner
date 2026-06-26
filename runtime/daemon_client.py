from __future__ import annotations

import json
from pathlib import Path
import socket
from typing import Any

from runtime.daemon_api import DEFAULT_DAEMON_SOCKET


def daemon_request(
    method: str,
    *,
    socket_path: str | Path = DEFAULT_DAEMON_SOCKET,
    timeout_s: float = 3.0,
) -> dict[str, Any]:
    request = {"method": str(method)}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(float(timeout_s))
            client.connect(str(socket_path))
            client.sendall(
                (json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8")
            )
            chunks: list[bytes] = []
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
    except FileNotFoundError as exc:
        raise RuntimeError(f"PenguinBurner daemon socket not found: {socket_path}") from exc
    except OSError as exc:
        raise RuntimeError(f"failed to connect to PenguinBurner daemon: {exc}") from exc

    line = b"".join(chunks).split(b"\n", 1)[0].decode("utf-8", errors="replace")
    if not line:
        raise RuntimeError("PenguinBurner daemon returned an empty response")
    response = json.loads(line)
    if not isinstance(response, dict):
        raise RuntimeError("PenguinBurner daemon returned an invalid response")
    if not response.get("ok"):
        raise RuntimeError(
            str(response.get("error") or "PenguinBurner daemon request failed")
        )
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("PenguinBurner daemon returned an invalid result")
    return result


def daemon_status(
    *,
    socket_path: str | Path = DEFAULT_DAEMON_SOCKET,
    timeout_s: float = 3.0,
) -> dict[str, Any]:
    return daemon_request("status", socket_path=socket_path, timeout_s=timeout_s)
