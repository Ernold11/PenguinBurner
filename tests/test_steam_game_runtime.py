from pathlib import Path

import pytest

import integrations.steam.game_runtime as game_runtime
from integrations.steam.game_runtime import (
    apply_game_runtime_profile,
    game_account_id,
    game_app_id,
    game_runtime_profile_argv,
    profile_argv_for_setting,
)
from integrations.steam.settings import (
    SteamGameSetting,
    store_steam_game_setting,
)
from integrations.steam.users import STEAMID64_BASE


ACCOUNT_ID = "78675700"


@pytest.fixture()
def steam_home(tmp_path: Path) -> Path:
    root = tmp_path / ".local" / "share" / "Steam"
    (root / "steamapps").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "userdata" / ACCOUNT_ID / "config").mkdir(parents=True)
    (root / "config" / "loginusers.vdf").write_text(
        '"users"\n{\n\t"%d"\n\t{\n\t\t"AccountName"\t\t"jan_pietek"\n'
        '\t\t"PersonaName"\t\t"jan.pietek"\n\t\t"MostRecent"\t\t"1"\n'
        '\t\t"Timestamp"\t\t"1"\n\t}\n}\n' % (STEAMID64_BASE + int(ACCOUNT_ID)),
        encoding="utf-8",
    )
    return tmp_path


def test_game_app_id_prefers_steam_app_id() -> None:
    assert game_app_id({"SteamAppId": "1089130"}) == "1089130"
    assert game_app_id({"STEAM_COMPAT_APP_ID": "42"}) == "42"
    assert game_app_id({"SteamAppId": "not-a-number"}) == ""
    assert game_app_id({}) == ""


def test_game_account_id_matches_steam_user_env(steam_home: Path) -> None:
    assert (
        game_account_id({"SteamUser": "jan_pietek"}, home=steam_home) == ACCOUNT_ID
    )
    # Unknown login name falls back to the active account.
    assert game_account_id({"SteamUser": "somebody"}, home=steam_home) == ACCOUNT_ID
    assert game_account_id({}, home=steam_home) == ACCOUNT_ID


def test_profile_argv_for_stock_is_none() -> None:
    assert profile_argv_for_setting(SteamGameSetting(mode="stock")) is None


def test_profile_argv_for_adaptive() -> None:
    argv = profile_argv_for_setting(SteamGameSetting(mode="adaptive"))
    assert argv == ["--auto-uv-profile", "latest", "--adaptive-auto-uv"]


def test_profile_argv_for_fixed_tier_resolves_profile(monkeypatch) -> None:
    monkeypatch.setattr(game_runtime, "read_auto_uv_profiles", lambda: [])
    monkeypatch.setattr(
        game_runtime,
        "resolve_profile_tier_profiles",
        lambda profiles: {"balanced": {"profile_id": "profile-123"}},
    )
    argv = profile_argv_for_setting(SteamGameSetting(mode="balanced"))
    assert argv == ["--auto-uv-profile", "profile-123"]


def test_profile_argv_for_unresolved_tier_is_none(monkeypatch) -> None:
    monkeypatch.setattr(game_runtime, "read_auto_uv_profiles", lambda: [])
    monkeypatch.setattr(
        game_runtime,
        "resolve_profile_tier_profiles",
        lambda profiles: {"balanced": None},
    )
    assert profile_argv_for_setting(SteamGameSetting(mode="balanced")) is None


def test_game_runtime_profile_argv_reads_setting(
    steam_home: Path, tmp_path: Path
) -> None:
    settings_path = tmp_path / "steam-game-settings.json"
    store_steam_game_setting(
        ACCOUNT_ID,
        "1089130",
        SteamGameSetting(mode="adaptive", overlay=True),
        path=settings_path,
    )
    env = {"SteamAppId": "1089130", "SteamUser": "jan_pietek"}
    resolved = game_runtime_profile_argv(
        env, home=steam_home, settings_path=settings_path
    )
    assert resolved is not None
    argv, app_id = resolved
    assert app_id == "1089130"
    assert "--adaptive-auto-uv" in argv


def test_game_runtime_profile_argv_none_without_setting(
    steam_home: Path, tmp_path: Path
) -> None:
    env = {"SteamAppId": "1089130", "SteamUser": "jan_pietek"}
    assert (
        game_runtime_profile_argv(
            env,
            home=steam_home,
            settings_path=tmp_path / "steam-game-settings.json",
        )
        is None
    )


def test_apply_calls_daemon_with_own_pid(
    steam_home: Path, tmp_path: Path, monkeypatch
) -> None:
    settings_path = tmp_path / "steam-game-settings.json"
    store_steam_game_setting(
        ACCOUNT_ID,
        "1089130",
        SteamGameSetting(mode="adaptive"),
        path=settings_path,
    )
    calls: list[dict] = []

    import runtime.daemon_client as daemon_client

    def fake_start(argv, *, watch_pid, app_id="", **kwargs):
        calls.append({"argv": argv, "watch_pid": watch_pid, "app_id": app_id})
        return {"started": True}

    monkeypatch.setattr(daemon_client, "start_game_runtime_profile", fake_start)
    env = {"SteamAppId": "1089130", "SteamUser": "jan_pietek"}
    assert apply_game_runtime_profile(
        env, home=steam_home, settings_path=settings_path
    )
    import os

    assert calls == [
        {
            "argv": ["--auto-uv-profile", "latest", "--adaptive-auto-uv"],
            "watch_pid": os.getpid(),
            "app_id": "1089130",
        }
    ]


def test_apply_soft_fails_when_daemon_unreachable(
    steam_home: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    settings_path = tmp_path / "steam-game-settings.json"
    store_steam_game_setting(
        ACCOUNT_ID,
        "1089130",
        SteamGameSetting(mode="adaptive"),
        path=settings_path,
    )
    import runtime.daemon_client as daemon_client

    def fake_start(*args, **kwargs):
        raise RuntimeError("daemon socket not found")

    monkeypatch.setattr(daemon_client, "start_game_runtime_profile", fake_start)
    env = {"SteamAppId": "1089130", "SteamUser": "jan_pietek"}
    assert not apply_game_runtime_profile(
        env, home=steam_home, settings_path=settings_path
    )
    assert "skipped" in capsys.readouterr().err


def test_launch_steam_game_validates_app_id(monkeypatch) -> None:
    import integrations.steam.process as process

    monkeypatch.setattr(process.shutil, "which", lambda name: "/usr/bin/steam")
    launched = []
    monkeypatch.setattr(
        process.subprocess,
        "Popen",
        lambda command, **kwargs: launched.append(command),
    )
    from integrations.steam.process import launch_steam_game

    assert launch_steam_game("3606110")
    assert launched == [["steam", "-applaunch", "3606110"]]
    assert not launch_steam_game("rm -rf /")
    assert len(launched) == 1
