from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest

from runtime import daemon_api
from runtime.daemon_client import daemon_status


@pytest.fixture(autouse=True)
def _isolate_last_runtime_state(tmp_path, monkeypatch):
    # The daemon prefers a persisted last-action file over the unit env autostart.
    # Point it at a non-existent tmp path so tests never read the host's real
    # /var/lib state (which would make a served daemon spuriously start a runtime).
    monkeypatch.setattr(
        daemon_api, "LAST_RUNTIME_STATE_PATH", tmp_path / "last-runtime.json"
    )


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


def test_daemon_start_auto_uv_streams_process_lines(monkeypatch) -> None:
    calls = []
    restarted = []

    class FakeProcess:
        pid = 1234
        stdout = iter(['{"event":"auto_uv_start"}\n', "human line\n"])

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    def fake_popen(command, **_kwargs):
        calls.append(list(command))
        return FakeProcess()

    monkeypatch.setattr(daemon_api.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        daemon_api,
        "_daemon_program_file",
        lambda: "/tmp/penguin_burner.py",
    )
    monkeypatch.setattr(daemon_api, "_stop_autostart_runtime_for_scan", lambda: None)
    monkeypatch.setattr(
        daemon_api,
        "_start_autostart_runtime_if_configured",
        lambda: restarted.append(None),
    )
    monkeypatch.setattr(daemon_api, "_ACTIVE_SCAN_PROCESS", None)
    monkeypatch.setattr(daemon_api, "_ACTIVE_SCAN_ARGV", [])

    payloads = list(
        daemon_api.stream_auto_uv_scan(
            {"gpu_index": 0, "auto_uv_mode": "performance"}
        )
    )

    assert calls == [
        [
            daemon_api.sys.executable,
            "/tmp/penguin_burner.py",
            "--auto-uv-voltage-scan",
            "--json-events",
            "--auto-uv-require-final-choice",
            "--gpu-index",
            "0",
            "--auto-uv-mode",
            "performance",
        ]
    ]
    assert payloads[0]["control"] == "started"
    assert payloads[1]["line"] == '{"event":"auto_uv_start"}\n'
    assert payloads[2]["line"] == "human line\n"
    assert payloads[3]["control"] == "finished"
    assert payloads[3]["exit_code"] == 0
    assert daemon_api._ACTIVE_SCAN_PROCESS is None
    assert restarted == [None]


def test_daemon_stream_disconnect_keeps_scan_tracked_for_monitor(monkeypatch) -> None:
    calls = []
    monitored = []

    class FakeProcess:
        pid = 1234
        stdout = iter(["first line\n", "second line\n"])

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    def fake_popen(command, **_kwargs):
        calls.append(list(command))
        return FakeProcess()

    monkeypatch.setattr(daemon_api.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        daemon_api,
        "_daemon_program_file",
        lambda: "/tmp/penguin_burner.py",
    )
    monkeypatch.setattr(daemon_api, "_stop_autostart_runtime_for_scan", lambda: None)
    monkeypatch.setattr(daemon_api, "_start_detached_scan_monitor", monitored.append)
    monkeypatch.setattr(daemon_api, "_ACTIVE_SCAN_PROCESS", None)
    monkeypatch.setattr(daemon_api, "_ACTIVE_SCAN_ARGV", [])

    stream = daemon_api.stream_auto_uv_scan({"gpu_index": 0})

    started = next(stream)
    line = next(stream)
    stream.close()

    assert calls
    assert started["control"] == "started"
    assert line["line"] == "first line\n"
    assert daemon_api._ACTIVE_SCAN_PROCESS is monitored[0]
    assert daemon_api.status_payload()["state"] == "auto_uv_scan_running"


def test_daemon_start_auto_uv_rejects_unknown_options() -> None:
    with pytest.raises(ValueError, match="unknown Auto-UV option: unknown"):
        list(daemon_api.stream_auto_uv_scan({"unknown": "value"}))


def test_daemon_start_runtime_profile_tracks_process(monkeypatch) -> None:
    calls = []

    class FakeProcess:
        pid = 4321

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    def fake_popen(command, **_kwargs):
        calls.append(list(command))
        return FakeProcess()

    monkeypatch.setattr(daemon_api.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        daemon_api,
        "_daemon_program_file",
        lambda: "/tmp/penguin_burner.py",
    )
    monkeypatch.setattr(daemon_api, "_ACTIVE_SCAN_PROCESS", None)
    monkeypatch.setattr(daemon_api, "_AUTOSTART_PROCESS", None)
    monkeypatch.setattr(daemon_api, "_AUTOSTART_ARGV", [])

    result = daemon_api.handle_request(
        {
            "method": "start_runtime_profile",
            "argv": ["--auto-uv-profile", "profile-a", "--silent-fan-curve"],
        }
    )

    assert result == {
        "started": True,
        "pid": 4321,
        "argv": ["--auto-uv-profile", "profile-a", "--silent-fan-curve"],
    }
    assert calls == [
        [
            daemon_api.sys.executable,
            "/tmp/penguin_burner.py",
            "--auto-uv-profile",
            "profile-a",
            "--silent-fan-curve",
        ]
    ]
    assert daemon_api.status_payload()["state"] == "runtime_profile_running"


def test_daemon_start_runtime_profile_rejects_unsupported_args() -> None:
    with pytest.raises(ValueError, match="unsupported runtime profile argument"):
        daemon_api.handle_request(
            {"method": "start_runtime_profile", "argv": ["--daemon-api", "/tmp/x"]}
        )


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
