from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest

from runtime import daemon_api
from runtime.daemon_client import daemon_status


def test_daemon_status_payload_is_stable() -> None:
    payload = daemon_api.handle_request({"method": "status"})

    assert payload["state"] == "idle"
    assert payload["active_job"] is None
    assert isinstance(payload["version"], str)


def test_daemon_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="unknown request field"):
        daemon_api.handle_request({"method": "status", "argv": ["--danger"]})


def test_daemon_rejects_unknown_methods() -> None:
    with pytest.raises(ValueError, match="unknown daemon method"):
        daemon_api.handle_request({"method": "run_cli"})


def test_daemon_client_status_roundtrip(tmp_path: Path) -> None:
    socket_path = tmp_path / "penguin-burnerd.sock"
    thread = threading.Thread(
        target=daemon_api.serve_daemon_api,
        args=(socket_path,),
        daemon=True,
    )
    thread.start()
    _wait_for_socket(socket_path)

    status = daemon_status(socket_path=socket_path)

    assert status["state"] == "idle"
    assert status["active_job"] is None


def test_daemon_socket_returns_error_for_malformed_request(tmp_path: Path) -> None:
    socket_path = tmp_path / "penguin-burnerd.sock"
    thread = threading.Thread(
        target=daemon_api.serve_daemon_api,
        args=(socket_path,),
        daemon=True,
    )
    thread.start()
    _wait_for_socket(socket_path)

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(socket_path))
        client.sendall(b"{bad json\n")
        response = json.loads(client.recv(4096).decode("utf-8"))

    assert response["ok"] is False
    assert "error" in response


def _wait_for_socket(path: Path) -> None:
    for _attempt in range(50):
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"socket was not created: {path}")
