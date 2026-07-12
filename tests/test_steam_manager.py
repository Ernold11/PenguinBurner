from pathlib import Path

import pytest

import integrations.steam.manager as manager_module
from integrations.steam.manager import SteamIntegrationManager
from integrations.steam.settings import (
    GAME_MODE_DEFAULT,
    GAME_MODE_STOCK,
    load_steam_game_settings,
)
from integrations.steam.users import STEAMID64_BASE


ACCOUNT_ID = "78675700"
APP_ID = "10"


class _FakeCdpClient:
    launch_options: dict[str, str] = {}
    terminated: list[str] = []
    compat_tool: dict[str, str] = {}
    fail = False

    def __init__(self, **kwargs):
        if type(self).fail:
            raise manager_module.SteamCdpError("no endpoint")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def app_launch_options(self, app_id, **kwargs):
        return type(self).launch_options.get(app_id)

    def set_app_launch_options(self, app_id, value, **kwargs):
        type(self).launch_options[app_id] = value
        return True

    def terminate_app_supported(self):
        return True

    def terminate_app(self, app_id):
        type(self).terminated.append(str(app_id))

    def compat_tool_selection_supported(self):
        return True

    def available_compat_tools(self, app_id):
        return (
            ("proton_experimental", "Proton Experimental"),
            ("GE-Proton10-34", "GE-Proton10-34"),
        )

    def specify_compat_tool(self, app_id, tool_name):
        type(self).compat_tool[str(app_id)] = str(tool_name)


@pytest.fixture()
def steam_home(tmp_path: Path) -> Path:
    root = tmp_path / ".local" / "share" / "Steam"
    steamapps = root / "steamapps"
    steamapps.mkdir(parents=True)
    (root / "config").mkdir()
    (root / "userdata" / ACCOUNT_ID / "config").mkdir(parents=True)
    (steamapps / f"appmanifest_{APP_ID}.acf").write_text(
        '"AppState"\n{\n\t"appid"\t\t"10"\n\t"name"\t\t"Test Game"\n'
        '\t"StateFlags"\t\t"4"\n\t"installdir"\t\t"TestGame"\n}\n',
        encoding="utf-8",
    )
    (root / "config" / "loginusers.vdf").write_text(
        '"users"\n{\n\t"%d"\n\t{\n\t\t"AccountName"\t\t"jan"\n'
        '\t\t"PersonaName"\t\t"jan.pietek"\n\t\t"MostRecent"\t\t"1"\n'
        '\t\t"Timestamp"\t\t"1"\n\t}\n}\n' % (STEAMID64_BASE + int(ACCOUNT_ID)),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture()
def manager(steam_home: Path, tmp_path: Path, monkeypatch) -> SteamIntegrationManager:
    _FakeCdpClient.launch_options = {APP_ID: "gamemoderun %command%"}
    _FakeCdpClient.terminated = []
    _FakeCdpClient.compat_tool = {}
    _FakeCdpClient.fail = False
    monkeypatch.setattr(manager_module, "SteamCdpClient", _FakeCdpClient)
    monkeypatch.setattr(manager_module, "steam_running", lambda: True)
    return SteamIntegrationManager(
        home=steam_home,
        settings_path=tmp_path / "steam-game-settings.json",
    )


def test_refresh_merges_library_settings_and_launch_options(manager) -> None:
    rows = manager.refresh()
    assert [row.game.app_id for row in rows] == [APP_ID]
    assert rows[0].launch_options == "gamemoderun %command%"
    assert rows[0].setting.mode == GAME_MODE_DEFAULT


def test_standing_mode_label_uses_rust_daemon_status(manager, monkeypatch) -> None:
    import runtime.daemon_client as daemon_client

    monkeypatch.setattr(
        daemon_client,
        "daemon_status",
        lambda **kwargs: {
            "active_job": {
                "runtime_mode": "static",
                "profile_id": "profile-balanced",
            }
        },
    )
    monkeypatch.setattr(
        manager_module,
        "resolve_auto_uv_profile",
        lambda selector: (Path("/tmp/profile.json"), {"profile_tier": "Balanced"}),
    )
    assert manager.standing_mode_label() == "Balanced"


def test_standing_mode_label_keeps_pre_game_action(manager, monkeypatch) -> None:
    import runtime.daemon_client as daemon_client

    monkeypatch.setattr(
        daemon_client,
        "daemon_status",
        lambda **kwargs: {
            "active_job": {"runtime_mode": "stock", "profile_id": ""},
            "game_runtime": {
                "active": True,
                "standing_runtime_mode": "adaptive",
                "standing_profile_id": "profile-balanced",
            },
        },
    )
    assert manager.standing_mode_label() == "Adaptive"


def test_set_mode_injects_and_persists(manager, tmp_path) -> None:
    manager.refresh()
    result = manager.set_game_mode(APP_ID, "balanced")
    assert result.ok
    assert result.launch_options == (
        "gamemoderun PENGUIN_BURNER --pb-overlay=0 %command%"
    )
    assert _FakeCdpClient.launch_options[APP_ID] == result.launch_options
    stored = load_steam_game_settings(tmp_path / "steam-game-settings.json")
    setting = stored[ACCOUNT_ID][APP_ID]
    assert setting.mode == "balanced"
    assert setting.original_launch_options == "gamemoderun %command%"


def test_overlay_toggle_updates_tokens(manager) -> None:
    manager.refresh()
    manager.set_game_mode(APP_ID, "adaptive")
    result = manager.set_game_overlay(APP_ID, True)
    assert result.ok
    assert result.launch_options == (
        "gamemoderun PENGUIN_BURNER --pb-overlay=1 %command%"
    )


def test_proton_selection_uses_steam_and_default_is_not_forced(manager) -> None:
    assert manager.available_compat_tools(APP_ID)[0] == (
        "proton_experimental",
        "Proton Experimental",
    )
    result = manager.set_game_compat_tool(APP_ID, "GE-Proton10-34")
    assert result.ok
    assert _FakeCdpClient.compat_tool[APP_ID] == "GE-Proton10-34"

    result = manager.set_game_compat_tool(APP_ID, "")
    assert result.ok
    assert _FakeCdpClient.compat_tool[APP_ID] == ""


def test_disabling_penguin_burner_restores_original_command(manager, tmp_path) -> None:
    manager.refresh()
    manager.set_game_enabled(APP_ID, True)
    manager.set_game_mode(APP_ID, "balanced")
    result = manager.set_game_enabled(APP_ID, False)
    assert result.ok
    assert _FakeCdpClient.launch_options[APP_ID] == "gamemoderun %command%"
    stored = load_steam_game_settings(tmp_path / "steam-game-settings.json")
    assert stored[ACCOUNT_ID][APP_ID].enabled is False


def test_stock_choice_persists_per_game(manager, tmp_path) -> None:
    """Stock is a first-class per-game mode: the system-wide profile stays
    tuned while this game pins the factory GPU state. It must round-trip,
    not silently migrate to Adaptive (which read as the combo snapping
    back when the user picked Stock)."""
    manager.refresh()
    manager.set_game_enabled(APP_ID, True)
    result = manager.set_game_mode(APP_ID, GAME_MODE_STOCK)
    assert result.ok
    assert result.launch_options == (
        "gamemoderun PENGUIN_BURNER --pb-overlay=0 %command%"
    )
    stored = load_steam_game_settings(tmp_path / "steam-game-settings.json")
    assert stored[ACCOUNT_ID][APP_ID].mode == GAME_MODE_STOCK


def test_raw_edit_validates_and_syncs_setting(manager, tmp_path) -> None:
    manager.refresh()
    manager.set_game_mode(APP_ID, "balanced")
    bad = manager.set_raw_launch_options(APP_ID, '%command% "broken')
    assert not bad.ok and "unbalanced" in bad.message
    good = manager.set_raw_launch_options(
        APP_ID, "mangohud PENGUIN_BURNER --pb-overlay=1 %command%"
    )
    assert good.ok
    stored = load_steam_game_settings(tmp_path / "steam-game-settings.json")
    assert stored[ACCOUNT_ID][APP_ID].overlay


def test_raw_edit_removing_wrapper_deactivates_mode(manager, tmp_path) -> None:
    manager.refresh()
    manager.set_game_mode(APP_ID, "balanced")
    result = manager.set_raw_launch_options(APP_ID, "gamemoderun %command%")
    assert result.ok
    stored = load_steam_game_settings(tmp_path / "steam-game-settings.json")
    assert stored[ACCOUNT_ID][APP_ID].mode == GAME_MODE_DEFAULT


def test_write_blocked_while_steam_runs_without_cdp(manager) -> None:
    manager.refresh()
    _FakeCdpClient.fail = True
    result = manager.set_game_mode(APP_ID, "balanced")
    assert not result.ok and "live apply" in result.message


def test_write_falls_back_to_disk_when_steam_stopped(
    manager, steam_home, monkeypatch
) -> None:
    manager.refresh()
    _FakeCdpClient.fail = True
    monkeypatch.setattr(manager_module, "steam_running", lambda: False)
    localconfig = (
        steam_home
        / ".local"
        / "share"
        / "Steam"
        / "userdata"
        / ACCOUNT_ID
        / "config"
        / "localconfig.vdf"
    )
    localconfig.write_text(
        '"UserLocalConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n'
        '\t\t\t"Steam"\n\t\t\t{\n\t\t\t\t"apps"\n\t\t\t\t{\n'
        f'\t\t\t\t\t"{APP_ID}"\n'
        "\t\t\t\t\t{\n"
        '\t\t\t\t\t\t"LaunchOptions"\t\t"gamemoderun %command%"\n'
        "\t\t\t\t\t}\n"
        "\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n",
        encoding="utf-8",
    )
    result = manager.set_game_mode(APP_ID, "balanced")
    assert result.ok and "config" in result.message
    assert "PENGUIN_BURNER --pb-overlay=0 %command%" in localconfig.read_text(
        encoding="utf-8"
    )


def test_hot_reapply_none_when_game_not_running(manager, monkeypatch) -> None:
    import runtime.daemon_client as daemon_client

    monkeypatch.setattr(
        daemon_client, "daemon_status", lambda **kwargs: {"state": "idle"}
    )
    assert manager.hot_reapply(APP_ID) is None


def test_hot_reapply_pushes_profile_to_running_game(manager, monkeypatch) -> None:
    import runtime.daemon_client as daemon_client
    import integrations.steam.game_runtime as game_runtime

    manager.refresh()
    manager.set_game_mode(APP_ID, "balanced")
    monkeypatch.setattr(
        daemon_client,
        "daemon_status",
        lambda **kwargs: {
            "state": "runtime_profile_running",
            "game_runtime": {
                "active": True,
                "watched": [{"pid": 4242, "app_id": APP_ID}],
            },
        },
    )
    monkeypatch.setattr(
        game_runtime, "read_auto_uv_profiles", lambda: []
    )
    monkeypatch.setattr(
        game_runtime,
        "resolve_profile_tier_profiles",
        lambda profiles: {"balanced": {"profile_id": "profile-9"}},
    )
    calls = []

    def fake_start(argv, *, watch_pid, app_id="", **kwargs):
        calls.append((list(argv), watch_pid, app_id))
        return {"started": True}

    monkeypatch.setattr(daemon_client, "start_game_runtime_profile", fake_start)
    result = manager.hot_reapply(APP_ID)
    assert result is not None and result.ok
    assert calls == [(["--auto-uv-profile", "profile-9"], 4242, APP_ID)]


def test_hot_reapply_default_resolves_standing_adaptive(manager, monkeypatch) -> None:
    import runtime.daemon_client as daemon_client

    manager.refresh()
    manager.set_game_mode(APP_ID, GAME_MODE_DEFAULT)
    monkeypatch.setattr(
        daemon_client,
        "daemon_status",
        lambda **kwargs: {
            "game_runtime": {
                "active": True,
                "watched": [{"pid": 4242, "app_id": APP_ID}],
                "standing_runtime_mode": "adaptive",
                "standing_profile_id": "performance-9",
            }
        },
    )
    calls = []

    def fake_start(argv, *, watch_pid, app_id="", **kwargs):
        calls.append((list(argv), watch_pid, app_id))
        return {"started": True}

    monkeypatch.setattr(daemon_client, "start_game_runtime_profile", fake_start)

    result = manager.hot_reapply(APP_ID)

    assert result is not None and result.ok
    assert calls == [
        (
            ["--auto-uv-profile", "performance-9", "--adaptive-auto-uv"],
            4242,
            APP_ID,
        )
    ]


def test_hot_reapply_tolerates_grace_window_exit(manager, monkeypatch) -> None:
    import runtime.daemon_client as daemon_client

    manager.refresh()
    manager.set_game_mode(APP_ID, "adaptive")
    monkeypatch.setattr(
        daemon_client,
        "daemon_status",
        lambda **kwargs: {
            "game_runtime": {
                "active": True,
                "watched": [{"pid": 4242, "app_id": APP_ID}],
            }
        },
    )

    def fake_start(argv, **kwargs):
        raise RuntimeError("watch_pid 4242 is not a running process")

    monkeypatch.setattr(daemon_client, "start_game_runtime_profile", fake_start)
    result = manager.hot_reapply(APP_ID)
    assert result is not None and result.ok
    assert "next launch" in result.message


def test_stop_game_terminates_via_cdp(manager) -> None:
    result = manager.stop_game(APP_ID)

    assert result.ok
    assert _FakeCdpClient.terminated == [APP_ID]


def test_stop_game_needs_live_apply(manager, monkeypatch) -> None:
    _FakeCdpClient.fail = True
    monkeypatch.setattr(manager_module, "cdp_available", lambda **kwargs: False)

    result = manager.stop_game(APP_ID)

    assert not result.ok
    assert "live apply" in result.message
    assert _FakeCdpClient.terminated == []


def test_stop_game_reports_steam_not_running(manager, monkeypatch) -> None:
    _FakeCdpClient.fail = True
    monkeypatch.setattr(manager_module, "steam_running", lambda: False)

    result = manager.stop_game(APP_ID)

    assert not result.ok
    assert result.message == "Steam is not running"


def test_stop_game_surfaces_real_cdp_error_when_live(manager, monkeypatch) -> None:
    # CDP is up (live apply initialized) but the call itself failed: the user
    # must see the actual error, not be told to initialize again.
    _FakeCdpClient.fail = True
    monkeypatch.setattr(manager_module, "cdp_available", lambda **kwargs: True)

    result = manager.stop_game(APP_ID)

    assert not result.ok
    assert "no endpoint" in result.message


def test_stop_game_rejects_bad_app_id(manager) -> None:
    assert not manager.stop_game("rm -rf /").ok
    assert _FakeCdpClient.terminated == []
