from __future__ import annotations

from types import SimpleNamespace

import pytest

from auto_uv.auto_uv_types import AutoUvError, AutoUvFinalChoiceDiscarded
from auto_uv.cli_runtime import (
    AutoUvForegroundDependencies,
    format_auto_uv_final_state,
    run_auto_uv_foreground_command,
    run_auto_uv_voltage_scan,
)
from penguin_burner_errors import NvmlError


def test_auto_uv_voltage_scan_wires_json_events_and_final_result() -> None:
    emitted = []
    logs = []
    checks = []
    build_calls = []

    args = SimpleNamespace(json_events=True)
    q2rtx_config = object()

    def fake_build_stability_config(*call_args, **kwargs):
        build_calls.append((call_args, kwargs))
        callback = kwargs["dependency_progress_callback"]
        callback({"step": "download"})
        return q2rtx_config

    def fake_runner(**kwargs):
        assert kwargs["q2rtx_config"] is q2rtx_config
        assert callable(kwargs["event_callback"])
        kwargs["event_callback"]("probe_progress", {"candidate_voltage_mv": 900})
        return SimpleNamespace(
            final_voltage_mv=875,
            lock_clock_mhz=2700,
            final_power_w=210.5,
            final_temperature_c=61.0,
            final_fan_speed_pct=40,
            stop_reason="verified",
            failed_candidate_voltage_mv=850,
        )

    run_auto_uv_voltage_scan(
        args,
        gpu_index=1,
        config_path="/tmp/config.toml",
        afterburner_runtime_options={"auto_uv_mode": "performance"},
        dependencies=AutoUvForegroundDependencies(
            require_auto_uv_initial_check=lambda **kwargs: checks.append(kwargs),
            build_stability_config=fake_build_stability_config,
            run_voltage_frequency_undervolt_main_loop=fake_runner,
            emit_json_event=lambda enabled, event, **payload: emitted.append(
                (enabled, event, payload)
            ),
            log=logs.append,
        ),
    )

    assert checks[0]["gpu_index"] == 1
    assert callable(checks[0]["log"])
    assert build_calls[0][1]["auto_install_q2rtx"] is True
    assert build_calls[0][1]["progress_context"] == "Auto-UV"
    assert build_calls[0][1]["dependency_text_progress"] is False
    assert emitted[0] == (
        True,
        "auto_uv_start",
        {"gpu_index": 1, "algorithm": "auto_uv"},
    )
    assert (True, "dependency_progress", {"step": "download"}) in emitted
    assert (True, "probe_progress", {"candidate_voltage_mv": 900}) in emitted
    assert emitted[-1] == (
        True,
        "final_result",
        {
            "voltage_mv": 875,
            "clock_mhz": 2700,
            "power_w": 210.5,
            "temperature_c": 61.0,
            "fan_pct": 40,
            "stop_reason": "verified",
            "failed_candidate_voltage_mv": 850,
        },
    )
    assert logs[-1].startswith("Auto-UV final state: 2700MHz@875mV")


def test_restore_defaults_command_ensures_afterburner_root_before_restore() -> None:
    calls = []
    args = SimpleNamespace(
        restore_defaults_from_config=True,
        auto_uv_voltage_scan=False,
    )

    def fake_ensure(config_path, runtime_options, **kwargs):
        calls.append(("ensure", config_path, runtime_options, kwargs))
        return {"afterburner_root": "/configured"}

    def fake_restore(**kwargs):
        calls.append(("restore", kwargs))

    run_auto_uv_foreground_command(
        args,
        gpu_index=0,
        config_path="/tmp/config.toml",
        afterburner_runtime_options={},
        interactive=True,
        dependencies=AutoUvForegroundDependencies(
            ensure_afterburner_root_configured=fake_ensure,
            restore_afterburner_defaults_from_config=fake_restore,
        ),
    )

    assert calls[0] == (
        "ensure",
        "/tmp/config.toml",
        {},
        {"gpu_index": 0, "interactive": True},
    )
    assert calls[1][0] == "restore"
    assert calls[1][1]["runtime_options"] == {"afterburner_root": "/configured"}


def test_auto_uv_foreground_command_translates_auto_uv_error() -> None:
    args = SimpleNamespace(
        restore_defaults_from_config=False,
        auto_uv_voltage_scan=True,
        json_events=False,
    )

    def fake_runner(**_kwargs):
        raise AutoUvError("driver rejected curve")

    with pytest.raises(NvmlError, match="driver rejected curve"):
        run_auto_uv_foreground_command(
            args,
            gpu_index=0,
            config_path="/tmp/config.toml",
            afterburner_runtime_options={},
            interactive=False,
            dependencies=AutoUvForegroundDependencies(
                require_auto_uv_initial_check=lambda **_kwargs: None,
                build_stability_config=lambda *_args, **_kwargs: object(),
                run_voltage_frequency_undervolt_main_loop=fake_runner,
            ),
        )


def test_auto_uv_foreground_command_logs_discarded_final_choice() -> None:
    args = SimpleNamespace(
        restore_defaults_from_config=False,
        auto_uv_voltage_scan=True,
        json_events=False,
    )
    logs = []

    def fake_runner(**_kwargs):
        raise AutoUvFinalChoiceDiscarded("discarded")

    run_auto_uv_foreground_command(
        args,
        gpu_index=0,
        config_path="/tmp/config.toml",
        afterburner_runtime_options={},
        interactive=False,
        dependencies=AutoUvForegroundDependencies(
            require_auto_uv_initial_check=lambda **_kwargs: None,
            build_stability_config=lambda *_args, **_kwargs: object(),
            run_voltage_frequency_undervolt_main_loop=fake_runner,
            log=logs.append,
        ),
    )

    assert logs == [
        "Auto-UV3: running the voltage-frequency undervolt main loop.",
        "discarded",
    ]


def test_format_auto_uv_final_state_uses_na_for_missing_metrics() -> None:
    text = format_auto_uv_final_state(
        SimpleNamespace(
            lock_clock_mhz=2700,
            final_voltage_mv=875,
            final_power_w=None,
            final_temperature_c=None,
            final_fan_speed_pct=None,
            stop_reason="verified",
            failed_candidate_voltage_mv=None,
        )
    )

    assert text == (
        "Auto-UV final state: 2700MHz@875mV "
        "power=n/aW temp=n/aC fan=n/a% stop_reason=verified "
        "failed_candidate=none"
    )
