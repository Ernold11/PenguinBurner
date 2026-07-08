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
    cleared = []

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
    monkeypatch.setattr(
        daemon_api,
        "_clear_stale_auto_uv_stop_request",
        lambda: cleared.append(None),
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
    assert cleared == [None]
    assert restarted == [None]


def test_daemon_start_auto_uv_reports_stale_stop_clear_failure(monkeypatch) -> None:
    calls = []

    def fake_popen(command, **_kwargs):
        calls.append(list(command))
        raise AssertionError("scan must not launch when stop cleanup fails")

    monkeypatch.setattr(daemon_api.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        daemon_api,
        "_daemon_program_file",
        lambda: "/tmp/penguin_burner.py",
    )
    monkeypatch.setattr(daemon_api, "_stop_autostart_runtime_for_scan", lambda: None)
    monkeypatch.setattr(
        daemon_api,
        "_clear_stale_auto_uv_stop_request",
        lambda: (_ for _ in ()).throw(PermissionError("denied")),
    )
    monkeypatch.setattr(daemon_api, "_ACTIVE_SCAN_PROCESS", None)
    monkeypatch.setattr(daemon_api, "_ACTIVE_SCAN_ARGV", [])

    payloads = list(daemon_api.stream_auto_uv_scan({"gpu_index": 0}))

    assert calls == []
    assert payloads == [
        {
            "ok": False,
            "error": "failed to clear stale Auto-UV stop request: denied",
        }
    ]


def test_daemon_stream_disconnect_stops_scan_without_final_choice(monkeypatch) -> None:
    calls = []
    monitored = []
    stop_requests = []
    signals = []

    class FakeProcess:
        pid = 1234
        stdout = iter(["first line\n", "second line\n"])

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

        def send_signal(self, signum):
            signals.append(signum)

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
        "_start_detached_scan_monitor",
        lambda process, **kwargs: monitored.append((process, kwargs)),
    )
    monkeypatch.setattr(
        daemon_api,
        "_write_auto_uv_stop_request",
        lambda **kwargs: stop_requests.append(kwargs),
    )
    monkeypatch.setattr(daemon_api, "_ACTIVE_SCAN_PROCESS", None)
    monkeypatch.setattr(daemon_api, "_ACTIVE_SCAN_ARGV", [])

    stream = daemon_api.stream_auto_uv_scan({"gpu_index": 0})

    started = next(stream)
    line = next(stream)
    stream.close()

    assert calls
    assert started["control"] == "started"
    assert line["line"] == "first line\n"
    assert daemon_api._ACTIVE_SCAN_PROCESS is monitored[0][0]
    assert daemon_api.status_payload()["state"] == "auto_uv_scan_running"
    assert stop_requests == [{"abort_final_choice": True}]
    assert signals == [daemon_api.signal.SIGINT]
    assert monitored[0][1] == {"kill_after_s": 30.0}


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


def test_daemon_probe_power_limit_support_writes_current_limit(monkeypatch) -> None:
    controllers = []

    class FakeController:
        def __init__(self, gpu_index: int) -> None:
            self.gpu_index = int(gpu_index)
            self.calls: list[int] = []
            self.closed = False
            controllers.append(self)

        def query_power_limits(self):
            return {
                "power_limit_w": 43,
                "power_limit_default_w": 61,
                "power_limit_min_w": 35,
                "power_limit_max_w": 80,
            }

        def apply_power_limit_w(self, power_limit_w):
            self.calls.append(int(power_limit_w))
            return int(power_limit_w)

        def close(self):
            self.closed = True

    monkeypatch.setattr(
        daemon_api,
        "_new_gpu_policy_controller",
        lambda gpu_index: FakeController(gpu_index),
    )

    result = daemon_api.handle_request(
        {"method": "probe_power_limit_support", "gpu_index": 2}
    )

    assert result["supported"] is True
    assert result["gpu_index"] == 2
    assert result["probe_power_limit_w"] == 43
    assert result["power_limits"]["power_limit_default_w"] == 61
    assert controllers[0].calls == [43]
    assert controllers[0].closed is True


def test_daemon_probe_power_limit_support_reports_setter_rejection(
    monkeypatch,
) -> None:
    class FakeController:
        def query_power_limits(self):
            return {
                "power_limit_w": 43,
                "power_limit_default_w": 61,
                "power_limit_min_w": 35,
                "power_limit_max_w": 80,
            }

        def apply_power_limit_w(self, power_limit_w):
            raise RuntimeError(
                "nvmlDeviceSetPowerManagementLimit failed with NVML error 3: "
                "Not Supported"
            )

        def close(self):
            pass

    monkeypatch.setattr(
        daemon_api,
        "_new_gpu_policy_controller",
        lambda _gpu_index: FakeController(),
    )

    result = daemon_api.handle_request(
        {"method": "probe_power_limit_support", "gpu_index": 0}
    )

    assert result["supported"] is False
    assert result["probe_power_limit_w"] == 43
    assert "Not Supported" in result["reason"]
    assert result["power_limits"]["power_limit_min_w"] == 35


def test_daemon_probe_power_limit_support_rejects_invalid_gpu_index() -> None:
    with pytest.raises(ValueError, match="invalid gpu_index"):
        daemon_api.handle_request(
            {"method": "probe_power_limit_support", "gpu_index": -1}
        )


def test_daemon_load_last_runtime_rebases_missing_program_file(
    monkeypatch,
    tmp_path: Path,
) -> None:
    current_program = tmp_path / "current" / "penguin_burner.py"
    current_program.parent.mkdir()
    current_program.write_text("# current\n", encoding="utf-8")
    missing_program = tmp_path / "removed" / "penguin_burner.py"
    daemon_api.LAST_RUNTIME_STATE_PATH.write_text(
        json.dumps(
            {
                "argv": ["--auto-uv-profile", "__stock__"],
                "program_file": str(missing_program),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        daemon_api,
        "_daemon_program_file",
        lambda: str(current_program),
    )

    assert daemon_api._load_last_runtime_argv() == (
        ["--auto-uv-profile", "__stock__"],
        str(current_program.resolve()),
    )


def test_daemon_autostart_rewrites_rebased_last_runtime_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    current_program = tmp_path / "active" / "penguin_burner.py"
    current_program.parent.mkdir()
    current_program.write_text("# current\n", encoding="utf-8")
    missing_program = tmp_path / "old-deployment" / "penguin_burner.py"
    daemon_api.LAST_RUNTIME_STATE_PATH.write_text(
        json.dumps(
            {
                "argv": ["--auto-uv-profile", "__stock__"],
                "program_file": str(missing_program),
            }
        ),
        encoding="utf-8",
    )
    calls = []

    class FakeProcess:
        pid = 1234

        def poll(self):
            return None

    def fake_popen(command, **_kwargs):
        calls.append(list(command))
        return FakeProcess()

    monkeypatch.setattr(daemon_api.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        daemon_api,
        "_daemon_program_file",
        lambda: str(current_program),
    )
    monkeypatch.setattr(daemon_api, "_AUTOSTART_PROCESS", None)
    monkeypatch.setattr(daemon_api, "_AUTOSTART_ARGV", [])

    daemon_api._start_autostart_runtime_if_configured()

    assert calls == [
        [
            daemon_api.sys.executable,
            str(current_program.resolve()),
            "--auto-uv-profile",
            "__stock__",
        ]
    ]
    persisted = json.loads(
        daemon_api.LAST_RUNTIME_STATE_PATH.read_text(encoding="utf-8")
    )
    assert persisted == {
        "argv": ["--auto-uv-profile", "__stock__"],
        "program_file": str(current_program.resolve()),
    }


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
