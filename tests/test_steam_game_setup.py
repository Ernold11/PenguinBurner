from latency_telemetry.steam_launch_check import (
    PENGUIN_BURNER_WRAPPER,
)
from latency_telemetry.steam_game_setup import (
    build_game_launch_options,
    default_steamapps_dirs,
    game_preset,
    installed_steam_game_presets,
)

# Synthetic app ids and presets: the setup helpers are game-agnostic, so the
# tests never name a real Steam title.
APP_ID = "1234567"
PRESET = game_preset(APP_ID, name="Test Game")


def test_game_preset_builds_arbitrary_app_id() -> None:
    preset = game_preset("9876543", name="Some Game")
    assert preset.app_id == "9876543"
    assert preset.name == "Some Game"
    assert preset.key == "app-9876543"
    assert (
        build_game_launch_options(preset, ingame_latency=True)
        == "PB_INGAME_LATENCY=1 PENGUIN_BURNER %command%"
    )


def test_game_preset_defaults_to_app_id_name() -> None:
    preset = game_preset("9876543")
    assert preset.name == "9876543"
    assert build_game_launch_options(preset) == "PENGUIN_BURNER %command%"


def test_build_launch_options_default_has_no_trace() -> None:
    opts = build_game_launch_options(PRESET, ingame_latency=False)
    assert opts == "PENGUIN_BURNER %command%"
    assert "DXVK_NVAPI_LOG_LEVEL=trace" not in opts
    assert "PROTON_LOG=1" not in opts
    assert "VK_ADD_IMPLICIT_LAYER_PATH" not in opts
    assert "VK_LOADER_LAYERS_ENABLE" not in opts


def test_build_launch_options_ingame_latency_adds_toggle_before_wrapper() -> None:
    opts = build_game_launch_options(PRESET, ingame_latency=True)
    assert "DXVK_NVAPI_LOG_LEVEL=trace" not in opts
    assert "PB_INGAME_LATENCY=1" in opts
    assert opts.index("PB_INGAME_LATENCY=1") < opts.index(PENGUIN_BURNER_WRAPPER)
    assert opts != build_game_launch_options(PRESET, ingame_latency=False)


def test_build_launch_options_overlay_adds_short_toggle_before_wrapper() -> None:
    opts = build_game_launch_options(PRESET, ingame_latency=True, overlay=True)

    assert opts == (
        "PENGUIN_BURNER_OVERLAY=1 PB_OVERLAY=1 "
        "PB_INGAME_LATENCY=1 PENGUIN_BURNER %command%"
    )


def test_installed_steam_game_presets_skip_steam_tools(tmp_path) -> None:
    steamapps = tmp_path / ".local/share/Steam/steamapps"
    steamapps.mkdir(parents=True)
    steamapps.joinpath("appmanifest_1.acf").write_text(
        '''
"AppState"
{
    "appid" "1"
    "name" "Installed Game"
}
''',
        encoding="utf-8",
    )
    steamapps.joinpath("appmanifest_2.acf").write_text(
        '''
"AppState"
{
    "appid" "2"
    "name" "Proton 10.0"
}
''',
        encoding="utf-8",
    )
    steamapps.joinpath("appmanifest_3.acf").write_text(
        '''
"AppState"
{
    "appid" "3"
    "name" "Steam Linux Runtime 4.0"
}
''',
        encoding="utf-8",
    )

    presets = installed_steam_game_presets(tmp_path)

    assert [(preset.app_id, preset.name) for preset in presets] == [
        ("1", "Installed Game")
    ]
    assert presets[0].key == "installed-1"


def test_default_steamapps_dirs_includes_extra_libraries(tmp_path) -> None:
    primary = tmp_path / ".local/share/Steam/steamapps"
    primary.mkdir(parents=True)
    library = tmp_path / "Games" / "SteamLibrary"
    library.joinpath("steamapps").mkdir(parents=True)
    primary.joinpath("libraryfolders.vdf").write_text(
        f'''
"libraryfolders"
{{
    "0"
    {{
        "path" "{library}"
    }}
}}
''',
        encoding="utf-8",
    )

    assert default_steamapps_dirs(tmp_path) == (
        primary,
        library / "steamapps",
    )
