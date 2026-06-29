from pathlib import Path

from overlay.telemetry.steam_launch_check import (
    BARE_PENGUIN_BURNER_WRAPPER,
    DEFAULT_REQUIRED_TOKENS,
    PENGUIN_BURNER_WRAPPER,
    check_launch_options,
    default_localconfig_paths,
    launch_options_from_localconfig,
    rewrite_launch_options,
    rewrite_launch_options_in_localconfig,
)

# Synthetic app id -- the helpers are game-agnostic, so the tests do not name a
# real Steam title.
APP_ID = "1234567"


def _localconfig(launch_options: str) -> str:
    return f'''
"UserLocalConfigStore"
{{
    "Software"
    {{
        "Valve"
        {{
            "Steam"
            {{
                "apps"
                {{
                    "{APP_ID}"
                    {{
                        "LastPlayed" "1781029254"
                        "LaunchOptions" "{launch_options}"
                    }}
                    "4180480"
                    {{
                        "LaunchOptions" "OTHER=1 %command%"
                    }}
                }}
            }}
        }}
    }}
}}
'''


def _localconfig_without_launch_options() -> str:
    return f'''
"UserLocalConfigStore"
{{
    "Software"
    {{
        "Valve"
        {{
            "Steam"
            {{
                "apps"
                {{
                    "{APP_ID}"
                    {{
                        "LastPlayed" "1781029254"
                    }}
                }}
            }}
        }}
    }}
}}
'''


def test_launch_options_from_localconfig_finds_app_block() -> None:
    launch_options = "PENGUIN_BURNER_LATENCY_LAYER=1 %command%"

    assert launch_options_from_localconfig(
        _localconfig(launch_options), APP_ID
    ) == launch_options


def test_rewrite_launch_options_in_localconfig_replaces_existing_value() -> None:
    requested = f"PB_OVERLAY=1 {PENGUIN_BURNER_WRAPPER} %command%"
    updated = rewrite_launch_options_in_localconfig(
        _localconfig("mangohud %command%"),
        APP_ID,
        requested,
    )

    assert updated is not None
    text, previous = updated
    assert previous == "mangohud %command%"
    assert launch_options_from_localconfig(text, APP_ID) == requested
    assert (
        launch_options_from_localconfig(text, "4180480")
        == "OTHER=1 %command%"
    )


def test_rewrite_launch_options_in_localconfig_adds_missing_value() -> None:
    requested = f"PB_OVERLAY=1 {PENGUIN_BURNER_WRAPPER} %command%"
    updated = rewrite_launch_options_in_localconfig(
        _localconfig_without_launch_options(),
        APP_ID,
        requested,
    )

    assert updated is not None
    text, previous = updated
    assert previous is None
    assert launch_options_from_localconfig(text, APP_ID) == requested


def test_rewrite_launch_options_writes_and_verifies_localconfig(tmp_path) -> None:
    path = tmp_path / "localconfig.vdf"
    path.write_text(_localconfig("mangohud %command%"), encoding="utf-8")
    requested = f"PB_OVERLAY=1 {PENGUIN_BURNER_WRAPPER} %command%"

    result = rewrite_launch_options(
        app_id=APP_ID,
        launch_options=requested,
        config_paths=[path],
        verify_delay_s=0.0,
    )

    assert result.ok
    assert result.config_path == path
    assert result.previous_launch_options == "mangohud %command%"
    assert result.current_launch_options == requested


def test_check_launch_options_accepts_default_probe_tokens(tmp_path) -> None:
    path = tmp_path / "localconfig.vdf"
    path.write_text(
        _localconfig(" ".join(DEFAULT_REQUIRED_TOKENS) + " gamemoderun %command%"),
        encoding="utf-8",
    )

    result = check_launch_options(
        app_id=APP_ID,
        required_tokens=DEFAULT_REQUIRED_TOKENS,
        config_paths=[path],
    )

    assert result.ok
    assert result.config_path == path
    assert result.missing_tokens == ()


def test_check_launch_options_accepts_bare_wrapper_token(tmp_path) -> None:
    path = tmp_path / "localconfig.vdf"
    path.write_text(
        _localconfig(f"PB_OVERLAY=1 {BARE_PENGUIN_BURNER_WRAPPER} %command%"),
        encoding="utf-8",
    )

    result = check_launch_options(
        app_id=APP_ID,
        required_tokens=DEFAULT_REQUIRED_TOKENS,
        config_paths=[path],
    )

    assert result.ok
    assert BARE_PENGUIN_BURNER_WRAPPER == PENGUIN_BURNER_WRAPPER
    assert result.missing_tokens == ()


def test_check_launch_options_reports_steam_overwrite_missing_token(tmp_path) -> None:
    path = tmp_path / "localconfig.vdf"
    path.write_text(
        _localconfig(
            "PENGUIN_BURNER_LATENCY_LAYER=1 "
            "gamemoderun %command%"
        ),
        encoding="utf-8",
    )

    result = check_launch_options(
        app_id=APP_ID,
        required_tokens=("VK_LOADER_LAYERS_ENABLE=VK_LAYER_PENGUINBURNER_latency",),
        config_paths=[path],
    )

    assert not result.ok
    assert result.missing_tokens == (
        "VK_LOADER_LAYERS_ENABLE=VK_LAYER_PENGUINBURNER_latency",
    )


def test_default_localconfig_paths_deduplicates_steam_symlink(tmp_path) -> None:
    real = tmp_path / ".local/share/Steam/userdata/1/config"
    real.mkdir(parents=True)
    config = real / "localconfig.vdf"
    config.write_text("", encoding="utf-8")
    alias = tmp_path / ".steam/steam/userdata/1/config"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(real)

    assert default_localconfig_paths(tmp_path) == [config]
