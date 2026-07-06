from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest

from runtime import daemon_api

# The fake-popen fixture patches subprocess.Popen module-wide (daemon_api and
# this module share the module object); keep the real one for spawning the
# watched "game" processes.
_REAL_POPEN = subprocess.Popen


@pytest.fixture(autouse=True)
def _isolate_game_state(tmp_path, monkeypatch):
    monkeypatch.setattr(
        daemon_api, "LAST_RUNTIME_STATE_PATH", tmp_path / "last-runtime.json"
    )
    monkeypatch.setattr(daemon_api, "_GAME_RESTORE_GRACE_S", 0.05)
    monkeypatch.setattr(daemon_api, "_AUTOSTART_PROCESS", None)
    monkeypatch.setattr(daemon_api, "_AUTOSTART_ARGV", [])
    monkeypatch.setattr(daemon_api, "_GAME_WATCHED_PIDS", {})
    monkeypatch.setattr(daemon_api, "_GAME_RUNTIME_ACTIVE", False)
    monkeypatch.setattr(daemon_api, "_GAME_PRIOR_RUNTIME_RUNNING", False)


def _standing_runtime_running(monkeypatch) -> None:
    """Simulate a runtime child already running (the user's standing action)."""
    monkeypatch.setattr(
        daemon_api, "_AUTOSTART_PROCESS", _FakeProcess(["python", "prog"])
    )


class _FakeProcess:
    def __init__(self, command):
        self.command = list(command)
        self.pid = 4321
        self._returncode = None

    def poll(self):
        return self._returncode

    def terminate(self):
        self._returncode = 0

    def kill(self):
        self._returncode = -9

    def wait(self, timeout=None):
        self._returncode = 0 if self._returncode is None else self._returncode
        return self._returncode


@pytest.fixture()
def fake_popen(monkeypatch):
    launched: list[list[str]] = []

    def popen(command, **_kwargs):
        launched.append(list(command))
        return _FakeProcess(command)

    monkeypatch.setattr(daemon_api.subprocess, "Popen", popen)
    monkeypatch.setattr(
        daemon_api, "_daemon_program_file", lambda: "/tmp/penguin_burner.py"
    )
    return launched


def _short_lived_process() -> subprocess.Popen:
    return _REAL_POPEN([sys.executable, "-c", "import time; time.sleep(0.4)"])


def test_start_game_runtime_profile_launches_and_watches(fake_popen) -> None:
    game = _short_lived_process()
    try:
        result = daemon_api.handle_request(
            {
                "method": "start_game_runtime_profile",
                "argv": ["--auto-uv-profile", "latest", "--adaptive-auto-uv"],
                "watch_pid": game.pid,
                "app_id": "1089130",
            }
        )
        assert result["started"] and result["watching_pid"] == game.pid
        assert fake_popen[-1][2:] == [
            "--auto-uv-profile",
            "latest",
            "--adaptive-auto-uv",
        ]
        status = daemon_api.handle_request({"method": "status"})
        assert status["game_runtime"]["watched"] == [
            {"pid": game.pid, "app_id": "1089130"}
        ]
    finally:
        game.wait()


def test_game_exit_restores_persisted_standing_action(
    fake_popen, tmp_path, monkeypatch
) -> None:
    _standing_runtime_running(monkeypatch)
    daemon_api.LAST_RUNTIME_STATE_PATH.write_text(
        json.dumps(
            {
                "argv": ["--auto-uv-profile", "standing-profile"],
                "program_file": "/tmp/penguin_burner.py",
            }
        ),
        encoding="utf-8",
    )
    game = _short_lived_process()
    daemon_api.handle_request(
        {
            "method": "start_game_runtime_profile",
            "argv": ["--auto-uv-profile", "game-profile"],
            "watch_pid": game.pid,
            "app_id": "10",
        }
    )
    game.wait()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if any("standing-profile" in " ".join(command) for command in fake_popen):
            break
        time.sleep(0.05)
    assert fake_popen[-1][2:] == ["--auto-uv-profile", "standing-profile"]
    # The per-game profile never becomes the persisted action.
    persisted = json.loads(
        daemon_api.LAST_RUNTIME_STATE_PATH.read_text(encoding="utf-8")
    )
    assert persisted["argv"] == ["--auto-uv-profile", "standing-profile"]
    status = daemon_api.handle_request({"method": "status"})
    assert "game_runtime" not in status


def test_game_exit_without_standing_action_stops_runtime(
    fake_popen, monkeypatch
) -> None:
    _standing_runtime_running(monkeypatch)
    game = _short_lived_process()
    daemon_api.handle_request(
        {
            "method": "start_game_runtime_profile",
            "argv": ["--auto-uv-profile", "game-profile"],
            "watch_pid": game.pid,
            "app_id": "10",
        }
    )
    game.wait()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        status = daemon_api.handle_request({"method": "status"})
        if "game_runtime" not in status:
            break
        time.sleep(0.05)
    assert "game_runtime" not in daemon_api.handle_request({"method": "status"})
    # Only the game profile was ever launched; nothing was restored.
    assert [command[2:] for command in fake_popen] == [
        ["--auto-uv-profile", "game-profile"]
    ]


def test_start_game_runtime_profile_validates_pid(fake_popen) -> None:
    with pytest.raises(ValueError, match="watch_pid"):
        daemon_api.handle_request(
            {
                "method": "start_game_runtime_profile",
                "argv": ["--auto-uv-profile", "latest"],
                "watch_pid": "not-a-pid",
            }
        )
    with pytest.raises(ValueError, match="not a running process"):
        daemon_api.handle_request(
            {
                "method": "start_game_runtime_profile",
                "argv": ["--auto-uv-profile", "latest"],
                "watch_pid": 2 ** 30,
            }
        )
    assert fake_popen == []


def test_start_game_runtime_profile_rejects_unsupported_args(fake_popen) -> None:
    with pytest.raises(ValueError, match="unsupported runtime profile argument"):
        daemon_api.handle_request(
            {
                "method": "start_game_runtime_profile",
                "argv": ["--daemon-api", "/tmp/x"],
                "watch_pid": 1,
            }
        )
    assert fake_popen == []


def test_game_exit_does_not_resurrect_a_stopped_runtime(
    fake_popen, tmp_path
) -> None:
    # The user stopped their runtime (daemon idle) but last-runtime.json still
    # holds the old action: game exit must NOT bring the stopped profile back.
    daemon_api.LAST_RUNTIME_STATE_PATH.write_text(
        json.dumps(
            {
                "argv": ["--auto-uv-profile", "stopped-profile"],
                "program_file": "/tmp/penguin_burner.py",
            }
        ),
        encoding="utf-8",
    )
    game = _short_lived_process()
    daemon_api.handle_request(
        {
            "method": "start_game_runtime_profile",
            "argv": ["--auto-uv-profile", "game-profile"],
            "watch_pid": game.pid,
            "app_id": "10",
        }
    )
    game.wait()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if "game_runtime" not in daemon_api.handle_request({"method": "status"}):
            break
        time.sleep(0.05)
    assert not any(
        "stopped-profile" in " ".join(command) for command in fake_popen
    )


def test_second_game_refcounts_restore(fake_popen, tmp_path, monkeypatch) -> None:
    _standing_runtime_running(monkeypatch)
    daemon_api.LAST_RUNTIME_STATE_PATH.write_text(
        json.dumps(
            {
                "argv": ["--auto-uv-profile", "standing-profile"],
                "program_file": "/tmp/penguin_burner.py",
            }
        ),
        encoding="utf-8",
    )
    short = _short_lived_process()
    long = _REAL_POPEN([sys.executable, "-c", "import time; time.sleep(1.5)"])
    daemon_api.handle_request(
        {
            "method": "start_game_runtime_profile",
            "argv": ["--auto-uv-profile", "game-a"],
            "watch_pid": short.pid,
            "app_id": "1",
        }
    )
    daemon_api.handle_request(
        {
            "method": "start_game_runtime_profile",
            "argv": ["--auto-uv-profile", "game-b"],
            "watch_pid": long.pid,
            "app_id": "2",
        }
    )
    short.wait()
    time.sleep(0.3)
    # First exit must not restore while the second game still runs.
    assert not any(
        "standing-profile" in " ".join(command) for command in fake_popen
    )
    long.wait()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if any("standing-profile" in " ".join(command) for command in fake_popen):
            break
        time.sleep(0.05)
    assert fake_popen[-1][2:] == ["--auto-uv-profile", "standing-profile"]
