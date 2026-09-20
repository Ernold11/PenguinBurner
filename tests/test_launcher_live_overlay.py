"""Live overlay contract independent of launcher storage and Qt."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from integrations.launchers import live_overlay
from integrations.launchers.registry import build_sources
from integrations.launchers.wrapper_manager import ApplyResult


@pytest.mark.parametrize("state", ["idle", "other", "unknown", "unwrapped", "probe_failed", "write_failed"])
def test_all_launchers_keep_saved_choice_on_unconfirmed_live_update(tmp_path, monkeypatch, state):
    path = tmp_path / "overlay-override"
    path.write_text("0")
    monkeypatch.setenv("PENGUIN_BURNER_OVERLAY_OVERRIDE", str(path))
    for source in build_sources(home=tmp_path):
        running = {"idle": frozenset(), "other": frozenset({"other"}), "unknown": None}.get(state, frozenset({"game"}))
        monkeypatch.setattr(source, "running_game_ids", lambda running=running: running)
        monkeypatch.setattr(source, "saved_overlay", lambda _id: True)
        keys = None if state == "probe_failed" else frozenset() if state == "unwrapped" else frozenset({f"{source.launcher_id}:game"})
        monkeypatch.setattr(live_overlay, "wrapped_game_keys", lambda keys=keys: keys)
        if state == "write_failed":
            monkeypatch.setattr("overlay.state.write_overlay_override", lambda enabled: False)
        for bulk in (False, True):
            result = (source.after_bulk_write("set_all_games_overlay", ApplyResult(True, "saved", applied_game_ids=("game",)))
                      if bulk else source.after_setting_write("game", "set_game_overlay"))
            if state in ("idle", "other"):
                assert result is None
            else:
                assert result is not None and not result.ok and "saved" in result.message
            assert path.read_text() == "0"
            assert source.saved_overlay("game") is True


def test_mixed_live_choices_are_not_arbitrarily_applied(tmp_path, monkeypatch):
    source = build_sources(home=tmp_path)[0]
    monkeypatch.setattr(source, "running_game_ids", lambda: frozenset({"one", "two"}))
    monkeypatch.setattr(source, "saved_overlay", lambda game_id: game_id == "one")
    monkeypatch.setattr(live_overlay, "wrapped_game_keys", lambda: frozenset({"steam:one", "steam:two"}))
    monkeypatch.setattr("overlay.state.write_overlay_override", lambda enabled: pytest.fail("Conflicting choices"))
    result = source.after_bulk_write("set_all_games_overlay", ApplyResult(True, "saved", applied_game_ids=("one", "two")))
    assert not result.ok and "conflicting" in result.message


@pytest.mark.parametrize("launcher", ["steam", "lutris", "heroic", "future"])
def test_host_probe_recognizes_real_wrapped_process_without_gpu_profile(monkeypatch, launcher):
    monkeypatch.setattr(live_overlay, "running_in_flatpak", lambda: False)
    key = f"{launcher}:987654321"
    env = dict(os.environ, PENGUIN_BURNER_SESSION_ID="overlay-contract-test")
    env.pop("PENGUIN_BURNER_GAME_KEY", None)
    if launcher == "steam":
        env["SteamAppId"] = "987654321"
    else:
        env["PENGUIN_BURNER_GAME_KEY"] = key
    with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE, env=env) as child:
        try:
            assert key in live_overlay.wrapped_game_keys()
        finally:
            child.stdin.close()
            child.wait(timeout=5)


def test_host_probe_excludes_helpers_unwrapped_processes_and_zombies(tmp_path):
    for pid, extra, state in ((1, b"", "S"), (2, b"PENGUIN_BURNER_TELEMETRY_SESSION=99", "S"),
                              (3, b"PENGUIN_BURNER_TELEMETRY_SESSION=3", "Z"),
                              (4, b"PENGUIN_BURNER_TELEMETRY_SESSION=4", "S")):
        proc = tmp_path / str(pid)
        proc.mkdir()
        (proc / "environ").write_bytes(b"PENGUIN_BURNER_GAME_KEY=lutris:" + str(pid).encode() + b"\0" + extra)
        (proc / "stat").write_text(f"{pid} (fake game) {state} 0")
    result = subprocess.run([sys.executable, "-c", live_overlay._WRAPPED_GAMES, str(tmp_path)], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == ["lutris:4"]


@pytest.mark.parametrize("payload", [None, "bad", "{}", '[1]', '["lutris:27"]'])
def test_probe_errors_stay_unknown_and_flatpak_uses_host_python(monkeypatch, payload):
    calls = []
    monkeypatch.setattr(live_overlay, "running_in_flatpak", lambda: True)
    monkeypatch.setenv("PENGUIN_BURNER_HOST_PYTHON", "/host/python")
    def run(command, **kwargs):
        calls.append(command)
        return None if payload is None else SimpleNamespace(returncode=0, stdout=payload)
    monkeypatch.setattr(live_overlay, "run_on_host", run)
    assert live_overlay.wrapped_game_keys() == (frozenset({"lutris:27"}) if payload == '["lutris:27"]' else None)
    assert calls[0][0] == "/host/python"
