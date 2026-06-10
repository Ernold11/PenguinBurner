from pathlib import Path

from latency_telemetry.steam_launch_check import (
    RE9_APP_ID,
    check_compat_tool,
    check_launch_options,
)
from latency_telemetry.steam_re9_setup import (
    PB_OVERLAY_WRAPPER,
    RE9_PATCHED_COMPAT_TOOL,
    RE9_PATCHED_EXTRA_TOKENS,
    RE9_PATCHED_LAUNCH_OPTIONS,
    SteamConfigError,
    apply_patched_re9_setup,
    set_compat_tool_in_config,
    set_launch_options_in_localconfig,
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


def _steam_config(tool_name: str) -> str:
    return f'''
"InstallConfigStore"
{{
    "Software"
    {{
        "Valve"
        {{
            "Steam"
            {{
                "CompatToolMapping"
                {{
                    "0"
                    {{
                        "name" "proton_hotfix"
                        "config" ""
                        "priority" "75"
                    }}
                    "{RE9_APP_ID}"
                    {{
                        "name" "{tool_name}"
                        "config" ""
                        "priority" "250"
                    }}
                }}
            }}
        }}
    }}
}}
'''


def test_set_launch_options_in_localconfig_replaces_re9_only() -> None:
    updated, changed = set_launch_options_in_localconfig(
        _localconfig("OLD=1 %command%"),
        app_id=RE9_APP_ID,
        launch_options=RE9_PATCHED_LAUNCH_OPTIONS,
    )

    assert changed
    assert RE9_PATCHED_LAUNCH_OPTIONS in updated
    assert (
        "VK_LOADER_LAYERS_ENABLE=VK_LAYER_PENGUINBURNER_latency,"
        "VK_LAYER_DXVK_NVAPI_reflex"
    ) in updated
    assert "native/latency_layer/build" in updated
    assert "third_party/dxvk-nvapi/build.layer" in updated
    assert "DXVK_NVAPI_VKREFLEX=1" in updated
    assert PB_OVERLAY_WRAPPER in updated
    assert '"4180480"' in updated
    assert 'OTHER=1 %command%' in updated


def test_set_compat_tool_in_config_replaces_re9_only() -> None:
    updated, changed = set_compat_tool_in_config(
        _steam_config("Proton-CachyOS Latest"),
        app_id=RE9_APP_ID,
        compat_tool=RE9_PATCHED_COMPAT_TOOL,
    )

    assert changed
    assert f'"name"\t\t"{RE9_PATCHED_COMPAT_TOOL}"' in updated
    assert '"name" "proton_hotfix"' in updated


def test_apply_patched_re9_setup_updates_files_and_writes_backups(tmp_path) -> None:
    localconfig = tmp_path / "localconfig.vdf"
    steam_config = tmp_path / "config.vdf"
    localconfig.write_text(_localconfig("OLD=1 %command%"), encoding="utf-8")
    steam_config.write_text(_steam_config("Proton-CachyOS Latest"), encoding="utf-8")

    result = apply_patched_re9_setup(
        localconfig_path=localconfig,
        steam_config_path=steam_config,
        check_running=False,
    )

    assert result.launch_options_changed
    assert result.compat_tool_changed
    assert result.localconfig_backup == tmp_path / "localconfig.vdf.pburn-bak"
    assert result.steam_config_backup == tmp_path / "config.vdf.pburn-bak"
    assert result.localconfig_backup.exists()
    assert result.steam_config_backup.exists()
    assert check_launch_options(
        app_id=RE9_APP_ID,
        required_tokens=RE9_PATCHED_EXTRA_TOKENS
        + (
            "VK_LOADER_LAYERS_ENABLE=VK_LAYER_PENGUINBURNER_latency,VK_LAYER_DXVK_NVAPI_reflex",
        ),
        config_paths=[localconfig],
    ).ok
    assert check_compat_tool(
        app_id=RE9_APP_ID,
        expected_tool=RE9_PATCHED_COMPAT_TOOL,
        config_paths=[steam_config],
    ).ok


def test_apply_patched_re9_setup_refuses_when_steam_is_running(
    tmp_path, monkeypatch
) -> None:
    localconfig = tmp_path / "localconfig.vdf"
    steam_config = tmp_path / "config.vdf"
    localconfig.write_text(_localconfig("OLD=1 %command%"), encoding="utf-8")
    steam_config.write_text(_steam_config("Proton-CachyOS Latest"), encoding="utf-8")
    monkeypatch.setattr(
        "latency_telemetry.steam_re9_setup.running_steam_processes",
        lambda: ("123 steam",),
    )

    try:
        apply_patched_re9_setup(
            localconfig_path=localconfig,
            steam_config_path=steam_config,
        )
    except SteamConfigError as exc:
        assert "Steam or a Wine game process is still running" in str(exc)
    else:
        raise AssertionError("expected SteamConfigError")

    assert "OLD=1" in localconfig.read_text(encoding="utf-8")


def test_apply_patched_re9_setup_waits_until_steam_exits(
    tmp_path, monkeypatch
) -> None:
    localconfig = tmp_path / "localconfig.vdf"
    steam_config = tmp_path / "config.vdf"
    localconfig.write_text(_localconfig("OLD=1 %command%"), encoding="utf-8")
    steam_config.write_text(_steam_config("Proton-CachyOS Latest"), encoding="utf-8")
    process_states = [("123 steam",), ()]
    sleep_calls: list[float] = []

    monkeypatch.setattr(
        "latency_telemetry.steam_re9_setup.running_steam_processes",
        lambda: process_states.pop(0),
    )
    monkeypatch.setattr(
        "latency_telemetry.steam_re9_setup.time.sleep",
        lambda seconds: sleep_calls.append(seconds),
    )

    result = apply_patched_re9_setup(
        localconfig_path=localconfig,
        steam_config_path=steam_config,
        wait=True,
        poll_interval_s=0.25,
    )

    assert sleep_calls == [0.25]
    assert result.launch_options_changed
    assert result.compat_tool_changed
    assert check_launch_options(
        app_id=RE9_APP_ID,
        required_tokens=RE9_PATCHED_EXTRA_TOKENS
        + (
            "VK_LOADER_LAYERS_ENABLE=VK_LAYER_PENGUINBURNER_latency,VK_LAYER_DXVK_NVAPI_reflex",
        ),
        config_paths=[localconfig],
    ).ok


def test_apply_patched_re9_setup_wait_timeout_refuses(tmp_path, monkeypatch) -> None:
    localconfig = tmp_path / "localconfig.vdf"
    steam_config = tmp_path / "config.vdf"
    localconfig.write_text(_localconfig("OLD=1 %command%"), encoding="utf-8")
    steam_config.write_text(_steam_config("Proton-CachyOS Latest"), encoding="utf-8")
    monkeypatch.setattr(
        "latency_telemetry.steam_re9_setup.running_steam_processes",
        lambda: ("123 steam",),
    )

    try:
        apply_patched_re9_setup(
            localconfig_path=localconfig,
            steam_config_path=steam_config,
            wait=True,
            wait_timeout_s=0.0,
        )
    except SteamConfigError as exc:
        assert "Steam or a Wine game process is still running" in str(exc)
    else:
        raise AssertionError("expected SteamConfigError")

    assert "OLD=1" in localconfig.read_text(encoding="utf-8")


def test_apply_patched_re9_setup_dry_run_does_not_write(tmp_path) -> None:
    localconfig = tmp_path / "localconfig.vdf"
    steam_config = tmp_path / "config.vdf"
    localconfig.write_text(_localconfig("OLD=1 %command%"), encoding="utf-8")
    steam_config.write_text(_steam_config("Proton-CachyOS Latest"), encoding="utf-8")

    result = apply_patched_re9_setup(
        localconfig_path=localconfig,
        steam_config_path=steam_config,
        dry_run=True,
        check_running=False,
    )

    assert result.dry_run
    assert result.launch_options_changed
    assert result.compat_tool_changed
    assert "OLD=1" in localconfig.read_text(encoding="utf-8")
    assert "Proton-CachyOS Latest" in steam_config.read_text(encoding="utf-8")
