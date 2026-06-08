from pathlib import Path
from types import SimpleNamespace

import pytest

from cli.arguments import parse_arguments
from cli.main_command_routing import (
    MainCommandRoutingDependencies,
    route_main_command,
)
from penguin_burner_errors import NvmlError


def _args(**overrides):
    values = {
        "clear_auto_uv_state": False,
        "fresh_auto_uv_scan": False,
        "auto_uv": False,
        "list_auto_uv_profiles": False,
        "json_events": False,
        "delete_auto_uv_profiles": [],
        "install_q2rtx": False,
        "check_latency_layer": False,
        "config": "/tmp/config.json",
        "gpu_index": None,
        "stability_test": False,
        "auto_uv_profile": "",
        "prefer_afterburner_curve": False,
        "auto_uv_require_final_choice": False,
        "auto_uv_voltage_scan": False,
        "restore_defaults_from_config": False,
        "silent_fan_curve": False,
        "export_lact_config": "",
        "lact_source": "auto-uv",
        "lact_gpu_id": "",
        "fan_curve_export": False,
        "lact_max_vf_offset_mhz": 1000,
        "dry_run": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _deps(**overrides):
    calls = {
        "logs": [],
        "prints": [],
        "clear": [],
        "stability": [],
        "foreground": [],
        "stop_runtime": [],
        "debug_options": [],
        "q2rtx_install": [],
    }

    def load_config(config_path):
        return (
            {
                "gpu": {"index": 0, "enable_persistence_mode": True},
                "fan": {"poll_interval_s": 1.0},
            },
            Path(config_path),
        )

    defaults = {
        "clear_auto_uv_state": lambda **kwargs: calls["clear"].append(kwargs),
        "load_config": load_config,
        "afterburner_root_has_imported_profiles": lambda root: bool(root),
        "run_q2rtx_install": lambda: calls["q2rtx_install"].append(True),
        "run_stability_test": lambda *args, **kwargs: calls["stability"].append(
            (args, kwargs)
        ),
        "load_afterburner_runtime_options": lambda config_path: {
            "afterburner_root": "",
            "afterburner_profile": "",
            "afterburner_device_profile": "",
        },
        "load_auto_uv_final_curve": lambda selector: {"path": "/tmp/final.json"},
        "running_under_systemd_service": lambda: False,
        "enable_stdio_capture": lambda *args, **kwargs: None,
        "stop_existing_penguin_burner_runtime": lambda **kwargs: calls[
            "stop_runtime"
        ].append(kwargs),
        "build_effective_afterburner_runtime_options": lambda args, stored: dict(
            stored
        ),
        "debug_effective_runtime_options": lambda **kwargs: calls[
            "debug_options"
        ].append(kwargs),
        "export_lact_config": lambda **kwargs: None,
        "run_profile_verification": lambda *args, **kwargs: None,
        "run_auto_uv_foreground_command": lambda *args, **kwargs: calls[
            "foreground"
        ].append((args, kwargs)),
        "run_afterburner_dry_run": lambda **kwargs: None,
        "read_auto_uv_profile_summaries": lambda: [{"id": "profile-a"}],
        "format_profile_table": lambda profiles: f"table:{profiles[0]['id']}",
        "delete_auto_uv_profiles": lambda selectors: [Path("/tmp/profile-a.json")],
        "log": calls["logs"].append,
        "print_fn": lambda *args, **kwargs: calls["prints"].append(
            (args, kwargs)
        ),
    }
    defaults.update(overrides)
    return MainCommandRoutingDependencies(**defaults), calls


def test_main_command_routing_lists_profiles_without_loading_runtime_config():
    deps, calls = _deps(
        load_config=lambda config_path: (_ for _ in ()).throw(
            AssertionError("config should not be loaded")
        )
    )

    result = route_main_command(
        args=_args(list_auto_uv_profiles=True),
        argv=["--list-auto-uv-profiles"],
        explicit_cli_args=True,
        interactive=False,
        dependencies=deps,
    )

    assert result.handled is True
    assert calls["prints"][0][0] == ("table:profile-a",)


def test_main_command_routing_checks_latency_layer_without_loading_runtime_config():
    deps, calls = _deps(
        load_config=lambda config_path: (_ for _ in ()).throw(
            AssertionError("config should not be loaded")
        ),
        check_latency_layer=lambda: {
            "ok": True,
            "layer_name": "VK_LAYER_PENGUINBURNER_latency",
            "launch_options": "PENGUIN_BURNER_LATENCY_LAYER=1 %command%",
        },
    )

    result = route_main_command(
        args=_args(check_latency_layer=True),
        argv=["--check-latency-layer"],
        explicit_cli_args=True,
        interactive=False,
        dependencies=deps,
    )

    assert result.handled is True
    assert "PenguinBurner latency layer: found" in calls["prints"][0][0][0]


def test_main_command_routing_rejects_clear_and_fresh_together():
    deps, _calls = _deps()

    with pytest.raises(NvmlError, match="choose only one"):
        route_main_command(
            args=_args(clear_auto_uv_state=True, fresh_auto_uv_scan=True),
            argv=[],
            explicit_cli_args=True,
            interactive=False,
            dependencies=deps,
        )


def test_main_command_routing_returns_runtime_inputs_for_normal_runtime():
    deps, calls = _deps(
        load_afterburner_runtime_options=lambda config_path: {
            "afterburner_root": "/afterburner",
            "afterburner_profile": "startup",
            "afterburner_device_profile": "VEN.cfg",
        },
        build_effective_afterburner_runtime_options=lambda args, stored: {
            **stored,
            "preserve_base_below_mv": 800,
        },
    )
    args = _args(gpu_index=2)

    result = route_main_command(
        args=args,
        argv=["--gpu-index", "2"],
        explicit_cli_args=True,
        interactive=True,
        dependencies=deps,
    )

    assert result.handled is False
    assert result.gpu_index == 2
    assert result.gpu_config["index"] == 2
    assert result.config_path == Path("/tmp/config.json")
    assert result.afterburner_runtime_options["afterburner_root"] == "/afterburner"
    assert result.afterburner_runtime_options["preserve_base_below_mv"] == 800
    assert result.auto_uv_final_curve_available is True
    assert result.had_persisted_afterburner_root is True
    assert calls["debug_options"][0]["gpu_index"] == 2


def test_main_command_routing_starts_default_auto_uv_foreground_when_no_runtime_profile():
    logs = []

    deps, calls = _deps(
        load_auto_uv_final_curve=lambda selector: None,
        afterburner_root_has_imported_profiles=lambda root: False,
        enable_stdio_capture=lambda *args, **kwargs: Path("/tmp/auto-uv.log"),
        log=logs.append,
    )
    args = _args()

    result = route_main_command(
        args=args,
        argv=[],
        explicit_cli_args=False,
        interactive=True,
        dependencies=deps,
    )

    assert result.handled is True
    assert args.auto_uv_voltage_scan is True
    assert calls["stop_runtime"] == [{"log": logs.append}]
    assert calls["foreground"][0][1]["interactive"] is True
    assert any("Auto-UV stdout/stderr log" in message for message in logs)
    assert any("starting the default foreground Auto-UV scan" in message for message in logs)


def test_main_command_routing_accepts_parsed_auto_uv_scan_args_without_legacy_flag():
    deps, calls = _deps(
        enable_stdio_capture=lambda *args, **kwargs: Path("/tmp/auto-uv.log"),
    )
    args = parse_arguments(
        [
            "--auto-uv-voltage-scan",
            "--json-events",
            "--auto-uv-require-final-choice",
            "--auto-uv-mode",
            "efficiency",
            "--auto-uv-max-drop-pct",
            "15",
            "--auto-uv-max-clock-drop-pct",
            "10",
            "--auto-uv-short-seconds",
            "10",
            "--auto-uv-memory-offset-mhz",
            "0",
            "--auto-uv-tail-rise-bins",
            "0",
        ]
    )

    result = route_main_command(
        args=args,
        argv=["--auto-uv-voltage-scan"],
        explicit_cli_args=True,
        interactive=False,
        dependencies=deps,
    )

    assert result.handled is True
    assert calls["foreground"]


def test_main_command_routing_runs_plain_stability_test_before_profile_setup():
    deps, calls = _deps(
        load_afterburner_runtime_options=lambda config_path: (_ for _ in ()).throw(
            AssertionError("afterburner runtime options should not be loaded")
        )
    )

    result = route_main_command(
        args=_args(stability_test=True),
        argv=["--stability-test"],
        explicit_cli_args=True,
        interactive=False,
        dependencies=deps,
    )

    assert result.handled is True
    assert calls["stability"][0][1]["gpu_index"] == 0
    assert calls["stability"][0][1]["config_path"] == Path("/tmp/config.json")
