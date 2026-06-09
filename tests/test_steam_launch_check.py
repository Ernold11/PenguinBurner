from pathlib import Path

from latency_telemetry.steam_launch_check import (
    RE9_APP_ID,
    RE9_REQUIRED_TOKENS,
    check_launch_options,
    default_localconfig_paths,
    launch_options_from_localconfig,
)


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
                    "{RE9_APP_ID}"
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


def test_launch_options_from_localconfig_finds_app_block() -> None:
    launch_options = "PENGUIN_BURNER_LATENCY_LAYER=1 %command%"

    assert launch_options_from_localconfig(
        _localconfig(launch_options), RE9_APP_ID
    ) == launch_options


def test_check_launch_options_accepts_current_re9_probe_tokens(tmp_path) -> None:
    path = tmp_path / "localconfig.vdf"
    path.write_text(
        _localconfig(" ".join(RE9_REQUIRED_TOKENS) + " gamemoderun %command%"),
        encoding="utf-8",
    )

    result = check_launch_options(
        app_id=RE9_APP_ID,
        required_tokens=RE9_REQUIRED_TOKENS,
        config_paths=[path],
    )

    assert result.ok
    assert result.config_path == path
    assert result.missing_tokens == ()


def test_check_launch_options_reports_steam_overwrite_missing_token(tmp_path) -> None:
    path = tmp_path / "localconfig.vdf"
    path.write_text(
        _localconfig(
            "PENGUIN_BURNER_LATENCY_LAYER=1 "
            "PENGUIN_BURNER_LATENCY_DEBUG_FLOW=1 "
            "PROTON_ENABLE_NVAPI=1 PROTON_HIDE_NVIDIA_GPU=0 "
            "DXVK_NVAPI_VKREFLEX=1 gamemoderun %command%"
        ),
        encoding="utf-8",
    )

    result = check_launch_options(
        app_id=RE9_APP_ID,
        required_tokens=("VKD3D_SWAPCHAIN_PRESENT_MODE=IMMEDIATE",),
        config_paths=[path],
    )

    assert not result.ok
    assert result.missing_tokens == ("VKD3D_SWAPCHAIN_PRESENT_MODE=IMMEDIATE",)


def test_default_localconfig_paths_deduplicates_steam_symlink(tmp_path) -> None:
    real = tmp_path / ".local/share/Steam/userdata/1/config"
    real.mkdir(parents=True)
    config = real / "localconfig.vdf"
    config.write_text("", encoding="utf-8")
    alias = tmp_path / ".steam/steam/userdata/1/config"
    alias.parent.mkdir(parents=True)
    alias.symlink_to(real)

    assert default_localconfig_paths(tmp_path) == [config]
