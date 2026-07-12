from pathlib import Path

from integrations.steam.settings import (
    GAME_MODE_ADAPTIVE,
    GAME_MODE_DEFAULT,
    GAME_MODE_STOCK,
    SteamGameSetting,
    load_steam_game_settings,
    normalize_game_mode,
    remove_steam_game_setting,
    steam_game_setting,
    store_steam_game_setting,
)


def test_store_and_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "steam-game-settings.json"
    setting = SteamGameSetting(
        mode=GAME_MODE_ADAPTIVE,
        overlay=True,
        original_launch_options="gamemoderun %command%",
        injected_launch_options="gamemoderun PB_OVERLAY=1 PENGUIN_BURNER %command%",
    )
    store_steam_game_setting("78675700", "1089130", setting, path=path)
    assert steam_game_setting("78675700", "1089130", path=path) == setting


def test_accounts_are_isolated(tmp_path: Path) -> None:
    path = tmp_path / "steam-game-settings.json"
    jan = SteamGameSetting(mode="balanced")
    marzena = SteamGameSetting(mode="efficiency", overlay=True)
    store_steam_game_setting("78675700", "1089130", jan, path=path)
    store_steam_game_setting("1255210572", "1089130", marzena, path=path)
    assert steam_game_setting("78675700", "1089130", path=path) == jan
    assert steam_game_setting("1255210572", "1089130", path=path) == marzena


def test_remove_setting_drops_empty_account(tmp_path: Path) -> None:
    path = tmp_path / "steam-game-settings.json"
    store_steam_game_setting("78675700", "1089130", SteamGameSetting(), path=path)
    remove_steam_game_setting("78675700", "1089130", path=path)
    assert load_steam_game_settings(path) == {}


def test_corrupt_file_loads_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "steam-game-settings.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_steam_game_settings(path) == {}


def test_normalize_game_mode_accepts_tier_aliases() -> None:
    assert normalize_game_mode("adaptive") == GAME_MODE_ADAPTIVE
    assert normalize_game_mode("stock") == GAME_MODE_STOCK
    assert normalize_game_mode("eff") == "efficiency"
    assert normalize_game_mode("perf") == "performance"
    assert normalize_game_mode("bogus") == GAME_MODE_DEFAULT
    assert normalize_game_mode(None) == GAME_MODE_DEFAULT


def test_setting_active_property() -> None:
    # No choice (default) is inactive; every explicit choice -- including
    # explicit stock -- persists and is active.
    assert not SteamGameSetting().active
    assert SteamGameSetting(mode="balanced").active
    assert SteamGameSetting(mode=GAME_MODE_STOCK).active
    assert SteamGameSetting(overlay=True).active
