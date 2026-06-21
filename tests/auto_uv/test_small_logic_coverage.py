"""Focused coverage tests for small pure-logic modules.

Targets previously-uncovered branches in:
- auto_uv/curve/performance_sweep_profile.py
- auto_uv/shared/probe_data_fields.py
- auto_uv/ui/probe_summary_ui_payload.py
- auto_uv/cli_runtime.py
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_uv.auto_uv_types import (
    AutoUvProbeSummary,
    FailureKind,
    FailureSeverity,
    StableRunDecision,
)
from auto_uv.cli_runtime import (
    AutoUvForegroundDependencies,
    run_auto_uv_voltage_scan,
)
from auto_uv.curve.performance_sweep_profile import (
    SweepProfileAnchor,
    build_sweep_profile_plan,
    interpolated_anchor_clock,
    lowest_stable_anchor_below,
    passed_attempt,
)
from auto_uv.curve.vf_curve_flattening import FlatteningRules
from auto_uv.shared.probe_data_fields import numeric_values, percent, read_field
from auto_uv.ui.probe_summary_ui_payload import probe_summary_ui_payload
from auto_uv.voltage_sweep_state import VoltageProbeOutcome
from common.penguin_burner_errors import NvmlError


# ---------------------------------------------------------------------------
# performance_sweep_profile.py
# ---------------------------------------------------------------------------


def _plan_point(voltage_mv: int, base_mhz: int, **extra) -> dict:
    point = {
        "voltage_mv": int(voltage_mv),
        "base_mhz": int(base_mhz),
        "target_mhz": int(base_mhz),
    }
    point.update(extra)
    return point


def test_build_sweep_profile_plan_preserves_base_when_flagged() -> None:
    """Line 109: preserve_base points keep base_mhz as their target."""
    anchors = [
        SweepProfileAnchor(voltage_mv=900, target_mhz=2600, source="lower"),
        SweepProfileAnchor(voltage_mv=950, target_mhz=2700, source="selected"),
    ]
    plan = build_sweep_profile_plan(
        selected_plan=[
            _plan_point(925, 2500, preserve_base=True),
            _plan_point(925, 2500),
        ],
        anchors=anchors,
        selected_voltage_mv=950,
        rules=FlatteningRules(clock_step_mhz=15),
    )

    preserved, interpolated = plan
    # preserve_base point: target == base, offset zeroed.
    assert preserved["target_mhz"] == 2500
    assert preserved["new_offset_mhz"] == 0
    # The non-preserved sibling at the same voltage gets interpolated up.
    assert interpolated["target_mhz"] > 2500


def test_interpolated_anchor_clock_returns_last_anchor_above_range() -> None:
    """Line 148: voltage above every anchor falls through to the last clock."""
    anchors = [
        SweepProfileAnchor(voltage_mv=900, target_mhz=2600, source="a"),
        SweepProfileAnchor(voltage_mv=950, target_mhz=2700, source="b"),
    ]
    result = interpolated_anchor_clock(
        anchors,
        voltage_mv=1000,
        rules=FlatteningRules(clock_step_mhz=15),
    )
    assert result == 2700


def test_lowest_stable_anchor_below_returns_none_without_lower_probes() -> None:
    """Line 162: no stable probe below the threshold voltage -> None."""
    probe = AutoUvProbeSummary(
        candidate_voltage_mv=950,
        lock_clock_mhz=2700,
        live_voltage_before_mv=950,
        live_voltage_after_mv=950,
        avg_voltage_mv=950.0,
        frames_per_run=1000,
        avg_seconds_per_run=10.0,
        avg_fps=100.0,
        min_fps=100.0,
        max_fps=100.0,
        avg_power_w=200.0,
        max_power_w=210.0,
        avg_temperature_c=60.0,
        max_temperature_c=62.0,
        avg_fan_speed_pct=35.0,
        max_fan_speed_pct=36.0,
        avg_core_clock_mhz=2700.0,
        efficiency_fps_per_w=0.5,
        efficiency_mhz_per_w=10.0,
        watts_per_mhz=0.1,
        used_companion_load=True,
        result_reason="stable run",
        log_path=Path("/tmp/q2rtx.log"),
    )
    # Threshold equals the only probe's voltage, so nothing is strictly below.
    assert lowest_stable_anchor_below([probe], voltage_mv=900) is None
    # Empty / None history is also covered.
    assert lowest_stable_anchor_below(None, voltage_mv=900) is None


def test_passed_attempt_false_for_non_voltage_probe_outcome() -> None:
    """Line 199: an outcome that is not a VoltageProbeOutcome is not 'passed'."""
    attempt = SimpleNamespace(outcome="not-an-outcome", candidate=object())
    assert passed_attempt(attempt) is False
    # Sanity: a genuine passed VoltageProbeOutcome returns True.
    good = SimpleNamespace(
        outcome=VoltageProbeOutcome(
            decision=StableRunDecision(
                True, FailureKind.NONE, FailureSeverity.PASS, "ok"
            ),
            measured_core_clock_mhz=2700.0,
            measured_voltage_mv=950.0,
            raw_probe=object(),
            raw_result=object(),
        ),
        candidate=object(),
    )
    assert passed_attempt(good) is True


# ---------------------------------------------------------------------------
# probe_data_fields.py
# ---------------------------------------------------------------------------


def test_numeric_values_skips_none_fields() -> None:
    """Line 22: records missing the field are skipped, present ones coerced."""
    records = [
        {"x": 1},
        {"x": None},
        {},  # field absent -> read_field returns None -> skipped
        {"x": "3.5"},
    ]
    assert numeric_values(records, "x") == [1.0, 3.5]


def test_read_field_dict_and_object_and_percent() -> None:
    assert read_field({"a": 7}, "a") == 7
    assert read_field(SimpleNamespace(a=9), "a") == 9
    assert read_field(SimpleNamespace(a=9), "missing") is None
    assert percent(50) == 0.5
    assert percent(-10) == 0.0


# ---------------------------------------------------------------------------
# probe_summary_ui_payload.py
# ---------------------------------------------------------------------------


def test_probe_summary_ui_payload_reads_dict_probe() -> None:
    """Line 55: dict-backed probe goes through the dict.get() branch."""
    probe = {
        "candidate_voltage_mv": 925,
        "lock_clock_mhz": 2700,
        "avg_core_clock_mhz": 2695.123,
        "avg_voltage_mv": 924.0,
        "loaded_qualified_sample_count": 12,
        "used_companion_load": True,
        "avg_fps": 142.4,
        "perf_cap_reason": None,
        "log_path": "/tmp/run.log",
    }
    payload = probe_summary_ui_payload(probe, stage="probe", decision="keep")

    assert payload["voltage_mv"] == 925
    assert payload["clock_mhz"] == 2700
    assert payload["measured_clock_mhz"] == 2695.12
    assert payload["loaded_qualified_sample_count"] == 12
    assert payload["used_companion_load"] is True
    assert payload["fps"] == 142.4
    assert payload["perf_cap_reason"] == ""
    assert payload["log_path"] == "/tmp/run.log"
    assert payload["stage"] == "probe"
    assert payload["decision"] == "keep"


# ---------------------------------------------------------------------------
# cli_runtime.py
# ---------------------------------------------------------------------------


def test_voltage_scan_wraps_initial_check_runtime_error_as_nvml_error() -> None:
    """Lines 107-108: a RuntimeError from the initial check becomes NvmlError."""

    def failing_initial_check(**_kwargs):
        raise RuntimeError("initial check unavailable")

    args = SimpleNamespace(json_events=False)
    with pytest.raises(NvmlError, match="initial check unavailable"):
        run_auto_uv_voltage_scan(
            args,
            gpu_index=0,
            config_path="/tmp/config.toml",
            auto_uv_runtime_options={},
            dependencies=AutoUvForegroundDependencies(
                require_auto_uv_initial_check=failing_initial_check,
                log=lambda *_a, **_k: None,
                emit_json_event=lambda *_a, **_k: None,
            ),
        )
