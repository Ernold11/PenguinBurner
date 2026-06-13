from pathlib import Path

from latency_telemetry.steam_launch_check import (
    PENGUIN_BURNER_WRAPPER,
    RE9_APP_ID,
    check_compat_tool,
    check_launch_options,
)
from latency_telemetry.steam_game_setup import (
    CRASH_4_APP_ID,
    CRASH_4_PRESET,
    DEFAULT_CACHYOS_COMPAT_TOOL,
    FIRST_LIGHT_APP_ID,
    FIRST_LIGHT_PRESET,
    HL2_RTX_APP_ID,
    HL2_RTX_PRESET,
    LEGO_BATMAN_APP_ID,
    LEGO_BATMAN_PRESET,
    QUAKE_II_RTX_APP_ID,
    QUAKE_II_RTX_PRESET,
    RE9_PRESET,
    STEAM_GAME_PRESETS_BY_KEY,
    SteamConfigError,
    apply_steam_game_setup,
    build_game_launch_options,
    installed_steam_game_presets,
    preferred_cachyos_compat_tool_name,
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


def _localconfig_without_launch_options(app_id: str) -> str:
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
                    "{app_id}"
                    {{
                        "LastPlayed" "1781029254"
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


def _localconfig_without_app(app_id: str) -> str:
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


def _steam_config_without_app_mapping() -> str:
    return '''
"InstallConfigStore"
{
    "Software"
    {
        "Valve"
        {
            "Steam"
            {
                "CompatToolMapping"
                {
                    "0"
                    {
                        "name" "proton_hotfix"
                        "config" ""
                        "priority" "75"
                    }
                }
            }
        }
    }
}
'''


def _pin_cachyos_tool(
    monkeypatch,
    tool_name: str = DEFAULT_CACHYOS_COMPAT_TOOL,
) -> None:
    import latency_telemetry.steam_game_setup as setup

    monkeypatch.setattr(
        setup,
        "preferred_cachyos_compat_tool_name",
        lambda: tool_name,
    )


def test_requested_games_are_supported_presets() -> None:
    expected = {
        "hl2-rtx": (HL2_RTX_PRESET, HL2_RTX_APP_ID),
        "lego-batman": (LEGO_BATMAN_PRESET, LEGO_BATMAN_APP_ID),
        "crash-4": (CRASH_4_PRESET, CRASH_4_APP_ID),
        "quake-ii-rtx": (QUAKE_II_RTX_PRESET, QUAKE_II_RTX_APP_ID),
    }

    for key, (preset, app_id) in expected.items():
        assert STEAM_GAME_PRESETS_BY_KEY[key] == preset
        assert preset.app_id == app_id
    assert build_game_launch_options(
        preset,
        ingame_latency=True,
    ) == "PB_INGAME_LATENCY=1 PENGUIN_BURNER %command% /WineDetectionEnabled:False"


def test_set_launch_options_in_localconfig_replaces_re9_only() -> None:
    launch_options = build_game_launch_options(RE9_PRESET)
    updated, changed = set_launch_options_in_localconfig(
        _localconfig("OLD=1 %command%"),
        app_id=RE9_APP_ID,
        launch_options=launch_options,
    )

    assert changed
    assert launch_options in updated
    assert PENGUIN_BURNER_WRAPPER in updated
    assert "VK_LOADER_LAYERS_ENABLE" not in updated
    assert "VK_ADD_IMPLICIT_LAYER_PATH" not in updated
    assert "DXVK_NVAPI_VKREFLEX=1" not in updated
    assert '"4180480"' in updated
    assert 'OTHER=1 %command%' in updated


def test_set_launch_options_in_localconfig_inserts_missing_value() -> None:
    updated, changed = set_launch_options_in_localconfig(
        _localconfig_without_launch_options(FIRST_LIGHT_APP_ID),
        app_id=FIRST_LIGHT_APP_ID,
        launch_options="PB_INGAME_LATENCY=1 PENGUIN_BURNER %command%",
    )

    assert changed
    assert '"LaunchOptions"\t\t"PB_INGAME_LATENCY=1 PENGUIN_BURNER %command%"' in updated


def test_set_launch_options_in_localconfig_inserts_missing_app_block() -> None:
    app_id = "3478870"
    launch_options = "PB_INGAME_LATENCY=1 PENGUIN_BURNER %command%"
    updated, changed = set_launch_options_in_localconfig(
        _localconfig_without_app(app_id),
        app_id=app_id,
        launch_options=launch_options,
    )

    assert changed
    assert f'"{app_id}"' in updated
    assert f'"LaunchOptions"\t\t"{launch_options}"' in updated
    assert '"4180480"' in updated
    assert 'OTHER=1 %command%' in updated


def test_set_compat_tool_in_config_replaces_re9_only() -> None:
    updated, changed = set_compat_tool_in_config(
        _steam_config("Proton-CachyOS Latest"),
        app_id=RE9_APP_ID,
        compat_tool=DEFAULT_CACHYOS_COMPAT_TOOL,
    )

    assert changed
    assert f'"name"\t\t"{DEFAULT_CACHYOS_COMPAT_TOOL}"' in updated
    assert '"name" "proton_hotfix"' in updated


def test_set_compat_tool_in_config_inserts_missing_app_mapping() -> None:
    updated, changed = set_compat_tool_in_config(
        _steam_config_without_app_mapping(),
        app_id=FIRST_LIGHT_APP_ID,
        compat_tool="proton-cachyos-11.0-20260521-slr-x86_64",
    )

    assert changed
    assert f'"{FIRST_LIGHT_APP_ID}"' in updated
    assert '"name"\t\t"proton-cachyos-11.0-20260521-slr-x86_64"' in updated
    assert '"priority"\t\t"250"' in updated


def test_preferred_cachyos_compat_tool_uses_newest_installed(tmp_path) -> None:
    def write_tool(dirname: str, name: str, version: str) -> None:
        root = tmp_path / ".local/share/Steam/compatibilitytools.d" / dirname
        root.mkdir(parents=True)
        root.joinpath("version").write_text(version, encoding="utf-8")
        root.joinpath("compatibilitytool.vdf").write_text(
            f'''
"compatibilitytools"
{{
  "compat_tools"
  {{
    "{name}"
    {{
      "install_path" "."
      "display_name" "{name}"
      "from_oslist" "windows"
      "to_oslist" "linux"
    }}
  }}
}}
''',
            encoding="utf-8",
        )

    write_tool(
        "Proton-CachyOS Latest",
        "Proton-CachyOS Latest",
        "1778931159 cachyos-11.0-20260506-slr",
    )
    write_tool(
        "proton-cachyos-11.0-20260521-slr-x86_64",
        "proton-cachyos-11.0-20260521-slr-x86_64",
        "1780144775 cachyos-11.0-20260521-slr",
    )

    assert (
        preferred_cachyos_compat_tool_name(tmp_path)
        == "proton-cachyos-11.0-20260521-slr-x86_64"
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


def test_apply_game_setup_updates_re9_files_and_writes_backups(
    tmp_path,
    monkeypatch,
) -> None:
    import latency_telemetry.steam_game_setup as setup

    _pin_cachyos_tool(monkeypatch)
    service_actions = []
    monkeypatch.setattr(
        setup,
        "install_ingame_latency_bridge_service",
        lambda: service_actions.append("on"),
    )
    monkeypatch.setattr(
        setup,
        "remove_ingame_latency_bridge_service",
        lambda: service_actions.append("off"),
    )
    localconfig = tmp_path / "localconfig.vdf"
    steam_config = tmp_path / "config.vdf"
    localconfig.write_text(_localconfig("OLD=1 %command%"), encoding="utf-8")
    steam_config.write_text(_steam_config("Proton-CachyOS Latest"), encoding="utf-8")

    result = apply_steam_game_setup(
        RE9_PRESET,
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
        required_tokens=(PENGUIN_BURNER_WRAPPER,),
        config_paths=[localconfig],
    ).ok
    assert check_compat_tool(
        app_id=RE9_APP_ID,
        expected_tool=DEFAULT_CACHYOS_COMPAT_TOOL,
        config_paths=[steam_config],
    ).ok
    assert service_actions == ["off"]


def test_apply_game_setup_refuses_when_steam_is_running(
    tmp_path, monkeypatch
) -> None:
    localconfig = tmp_path / "localconfig.vdf"
    steam_config = tmp_path / "config.vdf"
    localconfig.write_text(_localconfig("OLD=1 %command%"), encoding="utf-8")
    steam_config.write_text(_steam_config("Proton-CachyOS Latest"), encoding="utf-8")
    monkeypatch.setattr(
        "latency_telemetry.steam_game_setup.running_steam_processes",
        lambda: ("123 steam",),
    )

    try:
        apply_steam_game_setup(
            RE9_PRESET,
            localconfig_path=localconfig,
            steam_config_path=steam_config,
        )
    except SteamConfigError as exc:
        assert "Steam or a Wine game process is still running" in str(exc)
    else:
        raise AssertionError("expected SteamConfigError")

    assert "OLD=1" in localconfig.read_text(encoding="utf-8")


def test_apply_game_setup_waits_until_steam_exits(
    tmp_path, monkeypatch
) -> None:
    import latency_telemetry.steam_game_setup as setup

    _pin_cachyos_tool(monkeypatch)
    localconfig = tmp_path / "localconfig.vdf"
    steam_config = tmp_path / "config.vdf"
    localconfig.write_text(_localconfig("OLD=1 %command%"), encoding="utf-8")
    steam_config.write_text(_steam_config("Proton-CachyOS Latest"), encoding="utf-8")
    process_states = [("123 steam",), ()]
    sleep_calls: list[float] = []

    monkeypatch.setattr(
        "latency_telemetry.steam_game_setup.running_steam_processes",
        lambda: process_states.pop(0),
    )
    monkeypatch.setattr(
        "latency_telemetry.steam_game_setup.time.sleep",
        lambda seconds: sleep_calls.append(seconds),
    )
    service_actions = []
    monkeypatch.setattr(
        setup,
        "install_ingame_latency_bridge_service",
        lambda: service_actions.append("on"),
    )
    monkeypatch.setattr(
        setup,
        "remove_ingame_latency_bridge_service",
        lambda: service_actions.append("off"),
    )

    result = apply_steam_game_setup(
        RE9_PRESET,
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
        required_tokens=(PENGUIN_BURNER_WRAPPER,),
        config_paths=[localconfig],
    ).ok
    assert service_actions == ["off"]


def test_apply_game_setup_wait_timeout_refuses(tmp_path, monkeypatch) -> None:
    localconfig = tmp_path / "localconfig.vdf"
    steam_config = tmp_path / "config.vdf"
    localconfig.write_text(_localconfig("OLD=1 %command%"), encoding="utf-8")
    steam_config.write_text(_steam_config("Proton-CachyOS Latest"), encoding="utf-8")
    monkeypatch.setattr(
        "latency_telemetry.steam_game_setup.running_steam_processes",
        lambda: ("123 steam",),
    )

    try:
        apply_steam_game_setup(
            RE9_PRESET,
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


def test_apply_game_setup_dry_run_does_not_write(tmp_path, monkeypatch) -> None:
    _pin_cachyos_tool(monkeypatch)
    localconfig = tmp_path / "localconfig.vdf"
    steam_config = tmp_path / "config.vdf"
    localconfig.write_text(_localconfig("OLD=1 %command%"), encoding="utf-8")
    steam_config.write_text(_steam_config("Proton-CachyOS Latest"), encoding="utf-8")

    result = apply_steam_game_setup(
        RE9_PRESET,
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


def test_build_launch_options_default_has_no_trace() -> None:
    opts = build_game_launch_options(RE9_PRESET, ingame_latency=False)
    assert "DXVK_NVAPI_LOG_LEVEL=trace" not in opts
    assert "PROTON_LOG=1" not in opts
    assert "VK_ADD_IMPLICIT_LAYER_PATH" not in opts
    assert "VK_LOADER_LAYERS_ENABLE" not in opts
    assert PENGUIN_BURNER_WRAPPER in opts
    assert opts.rstrip().endswith("/WineDetectionEnabled:False")


def test_build_launch_options_experimental_adds_toggle_before_wrapper() -> None:
    opts = build_game_launch_options(RE9_PRESET, ingame_latency=True)
    # The trace env lives in the wrapper, not the Steam line; the line only
    # carries the toggle that tells the wrapper to enable trace.
    assert "DXVK_NVAPI_LOG_LEVEL=trace" not in opts
    assert "PB_INGAME_LATENCY=1" in opts
    assert opts.index("PB_INGAME_LATENCY=1") < opts.index(PENGUIN_BURNER_WRAPPER)
    assert opts != build_game_launch_options(RE9_PRESET, ingame_latency=False)


def test_build_launch_options_overlay_adds_short_toggle_before_wrapper() -> None:
    opts = build_game_launch_options(RE9_PRESET, ingame_latency=True, overlay=True)

    assert opts.startswith(
        "PENGUIN_BURNER_OVERLAY=1 PB_OVERLAY=1 "
        "PB_INGAME_LATENCY=1 PENGUIN_BURNER "
    )
    assert opts.rstrip().endswith("/WineDetectionEnabled:False")


def test_apply_experimental_writes_wrapper_and_verifies(tmp_path, monkeypatch) -> None:
    import latency_telemetry.steam_game_setup as setup

    _pin_cachyos_tool(monkeypatch)
    installed = []
    monkeypatch.setattr(
        setup, "install_ingame_latency_bridge_service", lambda: installed.append("on")
    )
    monkeypatch.setattr(
        setup, "remove_ingame_latency_bridge_service", lambda: installed.append("off")
    )
    localconfig = tmp_path / "localconfig.vdf"
    steam_config = tmp_path / "config.vdf"
    localconfig.write_text(_localconfig("OLD=1 %command%"), encoding="utf-8")
    steam_config.write_text(_steam_config("Proton-CachyOS Latest"), encoding="utf-8")

    result = apply_steam_game_setup(
        RE9_PRESET,
        localconfig_path=localconfig,
        steam_config_path=steam_config,
        check_running=False,
        ingame_latency=True,
    )

    written = localconfig.read_text(encoding="utf-8")
    assert PENGUIN_BURNER_WRAPPER in written
    assert "PB_INGAME_LATENCY=1" in written
    assert "DXVK_NVAPI_LOG_LEVEL=trace" not in result.format_text()
    assert installed == ["on"]


def test_apply_default_removes_bridge_service(tmp_path, monkeypatch) -> None:
    import latency_telemetry.steam_game_setup as setup

    _pin_cachyos_tool(monkeypatch)
    installed = []
    monkeypatch.setattr(
        setup, "install_ingame_latency_bridge_service", lambda: installed.append("on")
    )
    monkeypatch.setattr(
        setup, "remove_ingame_latency_bridge_service", lambda: installed.append("off")
    )
    localconfig = tmp_path / "localconfig.vdf"
    steam_config = tmp_path / "config.vdf"
    localconfig.write_text(_localconfig("OLD=1 %command%"), encoding="utf-8")
    steam_config.write_text(_steam_config("Proton-CachyOS Latest"), encoding="utf-8")

    apply_steam_game_setup(
        RE9_PRESET,
        localconfig_path=localconfig,
        steam_config_path=steam_config,
        check_running=False,
        ingame_latency=False,
    )

    assert "DXVK_NVAPI_LOG_LEVEL=trace" not in localconfig.read_text(encoding="utf-8")
    assert installed == ["off"]


def test_apply_first_light_setup_adds_trace_wrapper_and_mapping(
    tmp_path,
    monkeypatch,
) -> None:
    import latency_telemetry.steam_game_setup as setup

    tool_name = "proton-cachyos-11.0-20260521-slr-x86_64"
    _pin_cachyos_tool(monkeypatch, tool_name)
    installed = []
    monkeypatch.setattr(
        setup, "install_ingame_latency_bridge_service", lambda: installed.append("on")
    )
    monkeypatch.setattr(
        setup, "remove_ingame_latency_bridge_service", lambda: installed.append("off")
    )
    localconfig = tmp_path / "localconfig.vdf"
    steam_config = tmp_path / "config.vdf"
    localconfig.write_text(
        _localconfig_without_launch_options(FIRST_LIGHT_APP_ID),
        encoding="utf-8",
    )
    steam_config.write_text(_steam_config_without_app_mapping(), encoding="utf-8")

    result = apply_steam_game_setup(
        FIRST_LIGHT_PRESET,
        localconfig_path=localconfig,
        steam_config_path=steam_config,
        check_running=False,
        ingame_latency=True,
    )

    assert result.game_name == "007 First Light"
    assert result.app_id == FIRST_LIGHT_APP_ID
    assert result.compat_tool == tool_name
    written = localconfig.read_text(encoding="utf-8")
    assert "PB_INGAME_LATENCY=1" in written
    assert PENGUIN_BURNER_WRAPPER in written
    assert "DXVK_NVAPI_LOG_LEVEL=trace" not in written
    assert check_compat_tool(
        app_id=FIRST_LIGHT_APP_ID,
        expected_tool=tool_name,
        config_paths=[steam_config],
    ).ok
    assert installed == ["on"]


def test_apply_overlay_setup_writes_short_overlay_toggle(
    tmp_path,
    monkeypatch,
) -> None:
    import latency_telemetry.steam_game_setup as setup

    _pin_cachyos_tool(monkeypatch)
    installed = []
    monkeypatch.setattr(
        setup, "install_ingame_latency_bridge_service", lambda: installed.append("on")
    )
    monkeypatch.setattr(
        setup, "remove_ingame_latency_bridge_service", lambda: installed.append("off")
    )
    localconfig = tmp_path / "localconfig.vdf"
    steam_config = tmp_path / "config.vdf"
    localconfig.write_text(_localconfig("OLD=1 %command%"), encoding="utf-8")
    steam_config.write_text(_steam_config("Proton-CachyOS Latest"), encoding="utf-8")

    result = apply_steam_game_setup(
        RE9_PRESET,
        localconfig_path=localconfig,
        steam_config_path=steam_config,
        check_running=False,
        ingame_latency=True,
        overlay=True,
    )

    written = localconfig.read_text(encoding="utf-8")
    assert "PENGUIN_BURNER_OVERLAY=1" in written
    assert "PB_OVERLAY=1" in written
    assert "PB_INGAME_LATENCY=1" in written
    assert result.launch_options.startswith(
        "PENGUIN_BURNER_OVERLAY=1 PB_OVERLAY=1 PB_INGAME_LATENCY=1 "
    )
    assert installed == ["on"]
