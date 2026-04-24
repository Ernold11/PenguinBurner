from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

from auto_uv.curve_planning import (
    _build_descended_plan,
    _next_search_candidate_voltage_mv,
    _unsafe_min_search_voltage_mv,
    _validate_auto_uv_source_plan,
)
from auto_uv.fan_tuning import build_auto_uv_fan_payload
from auto_uv.models import AutoUvError, AutoUvProbeSummary
from auto_uv.probe_metrics import _temperature_normalized_efficiency_delta
from auto_uv.scan import (
    _core_clock_below_floor,
    _is_power_up_efficiency_down_regression,
    _probe_failure_should_mark_voltage_unsafe,
    _target_core_clock_floor,
    _telemetry_sample_is_busy,
)


def _plan() -> list[dict]:
    return [
        {
            "index": index,
            "voltage_mv": voltage_mv,
            "base_mhz": 1000 + index * 20,
            "target_mhz": 1000 + index * 20,
            "new_offset_mhz": 0,
        }
        for index, voltage_mv in enumerate(range(800, 1025, 25))
    ]


def _probe(
    *,
    requested_mv: int,
    measured_mv: float,
    fps: float,
    power_w: float,
    temp_c: float,
) -> AutoUvProbeSummary:
    return AutoUvProbeSummary(
        candidate_voltage_mv=requested_mv,
        lock_clock_mhz=2700,
        live_voltage_before_mv=None,
        live_voltage_after_mv=int(round(measured_mv)),
        avg_voltage_mv=float(measured_mv),
        frames_per_run=1000,
        avg_seconds_per_run=10.0,
        avg_fps=float(fps),
        min_fps=float(fps),
        max_fps=float(fps),
        avg_power_w=float(power_w),
        max_power_w=float(power_w),
        avg_temperature_c=float(temp_c),
        max_temperature_c=float(temp_c),
        avg_fan_speed_pct=40.0,
        max_fan_speed_pct=40.0,
        avg_core_clock_mhz=2600.0,
        efficiency_fps_per_w=float(fps) / float(power_w),
        efficiency_mhz_per_w=2600.0 / float(power_w),
        watts_per_mhz=float(power_w) / 2600.0,
        used_companion_load=False,
        result_reason="passed",
        log_path=Path("synthetic.log"),
    )


def test_descended_plan_flattens_only_candidate_and_higher_bins() -> None:
    source_plan = _plan()

    descended = _build_descended_plan(
        source_plan,
        lock_clock_mhz=2500,
        candidate_voltage_mv=900,
    )

    below = [item for item in descended if int(item["voltage_mv"]) < 900]
    at_or_above = [item for item in descended if int(item["voltage_mv"]) >= 900]
    assert below
    assert at_or_above
    assert all(int(item["target_mhz"]) == int(item["base_mhz"]) for item in below)
    assert all(int(item["target_mhz"]) == 2500 for item in at_or_above)


def test_descended_plan_rejects_nonexistent_voltage_bin() -> None:
    try:
        _build_descended_plan(_plan(), lock_clock_mhz=2500, candidate_voltage_mv=887)
    except AutoUvError as exc:
        assert "not an editable V/F bin" in str(exc)
    else:
        raise AssertionError("expected AutoUvError")


def test_candidate_search_respects_failed_floor_and_unsafe_voltage() -> None:
    source_plan = _plan()
    unsafe_floor, next_safe = _unsafe_min_search_voltage_mv(
        plan=source_plan,
        start_voltage_mv=1000,
        unsafe_entries=[{"candidate_voltage_mv": 875}],
    )

    candidate = _next_search_candidate_voltage_mv(
        plan=source_plan,
        start_voltage_mv=1000,
        stable_voltage_mv=950,
        reference_actual_voltage_mv=950.0,
        preserve_vanilla_below_mv=None,
        min_search_voltage_mv=next_safe,
        failed_floor_voltage_mv=unsafe_floor,
    )

    assert unsafe_floor == 875
    assert next_safe == 900
    assert candidate == 900
    assert candidate > unsafe_floor


def test_invalid_auto_uv_plan_is_rejected_without_gpu_access() -> None:
    bad_plan = [
        {
            "index": 0,
            "voltage_mv": 900,
            "base_mhz": 0,
            "target_mhz": 1200,
            "new_offset_mhz": 0,
        },
        {
            "index": 1,
            "voltage_mv": 925,
            "base_mhz": 1100,
            "target_mhz": 1200,
            "new_offset_mhz": 100,
        },
        {
            "index": 2,
            "voltage_mv": 950,
            "base_mhz": 1150,
            "target_mhz": 1200,
            "new_offset_mhz": 50,
        },
    ]

    try:
        _validate_auto_uv_source_plan(bad_plan)
    except AutoUvError as exc:
        assert "invalid editable point" in str(exc)
    else:
        raise AssertionError("expected AutoUvError")


def test_target_core_clock_floor_uses_lock_clock_when_probe_clock_is_lower() -> None:
    floor_mhz, base_mhz = _target_core_clock_floor(
        lock_clock_mhz=2760,
        initial_probe_clock_mhz=2606.7,
        min_performance_core_clock_pct=90.0,
        enforce_target_core_clock_floor=True,
    )

    assert base_mhz == 2760.0
    assert floor_mhz == 2484.0


def test_target_core_clock_floor_uses_probe_clock_when_it_is_higher() -> None:
    floor_mhz, base_mhz = _target_core_clock_floor(
        lock_clock_mhz=2760,
        initial_probe_clock_mhz=2800.0,
        min_performance_core_clock_pct=90.0,
        enforce_target_core_clock_floor=True,
    )

    assert base_mhz == 2800.0
    assert floor_mhz == 2520.0


def test_telemetry_sample_busy_check_ignores_idle_low_power_samples() -> None:
    idle_sample = SimpleNamespace(power_w=15.0, gpu_util_pct=0.0)
    busy_power_sample = SimpleNamespace(power_w=260.0, gpu_util_pct=0.0)
    busy_util_sample = SimpleNamespace(power_w=15.0, gpu_util_pct=80.0)

    assert _telemetry_sample_is_busy(idle_sample, busy_power_floor_w=140.0) is False
    assert (
        _telemetry_sample_is_busy(busy_power_sample, busy_power_floor_w=140.0) is True
    )
    assert _telemetry_sample_is_busy(busy_util_sample, busy_power_floor_w=140.0) is True


def test_timedemo_live_stall_does_not_mark_voltage_unsafe() -> None:
    assert (
        _probe_failure_should_mark_voltage_unsafe(
            "timedemo-live-stall idle=20.0s stall=15.0s completed=1"
        )
        is False
    )
    assert (
        _probe_failure_should_mark_voltage_unsafe(
            "telemetry-live-core_clock current=585.0MHz floor=2476.8MHz"
        )
        is True
    )


def test_core_clock_floor_allows_one_small_clock_tolerance() -> None:
    assert _core_clock_below_floor(2467.0, 2470.5) is False
    assert _core_clock_below_floor(2464.0, 2470.5) is True


def test_temperature_normalized_efficiency_tracks_ignored_driver_voltage() -> None:
    previous = _probe(
        requested_mv=1000,
        measured_mv=975.0,
        fps=170.0,
        power_w=295.0,
        temp_c=62.0,
    )
    candidate = _probe(
        requested_mv=985,
        measured_mv=974.0,
        fps=166.0,
        power_w=300.0,
        temp_c=64.0,
    )

    delta = _temperature_normalized_efficiency_delta(previous, candidate)

    assert delta["requested_voltage_drop_mv"] == 15.0
    assert delta["measured_voltage_drop_mv"] == 1.0
    assert delta["measured_voltage_close_to_requested"] is False
    assert delta["improved"] is False


def test_power_up_efficiency_down_regression_requires_real_measured_drop() -> None:
    previous = _probe(
        requested_mv=900,
        measured_mv=890.0,
        fps=170.0,
        power_w=240.0,
        temp_c=62.0,
    )
    candidate = _probe(
        requested_mv=895,
        measured_mv=885.0,
        fps=170.0,
        power_w=270.0,
        temp_c=64.0,
    )
    delta = _temperature_normalized_efficiency_delta(previous, candidate)

    assert _is_power_up_efficiency_down_regression(previous, candidate, delta) is True

    no_drop_delta = dict(delta)
    no_drop_delta["measured_voltage_drop_mv"] = 0.0
    assert (
        _is_power_up_efficiency_down_regression(previous, candidate, no_drop_delta)
        is False
    )


def test_auto_uv_fan_curve_blocks_if_final_load_is_too_hot() -> None:
    final_probe = _probe(
        requested_mv=900,
        measured_mv=890.0,
        fps=170.0,
        power_w=240.0,
        temp_c=76.0,
    )
    final_probe.max_temperature_c = 76.0

    payload = build_auto_uv_fan_payload(final_probe=final_probe, probes=[final_probe])

    assert payload is not None
    assert payload["fan_curve_blocked"] is True
    assert payload["block_reason"] == "stock-load-temperature-too-high"


def test_auto_uv_fan_curve_keeps_zero_rpm_and_emergency_points() -> None:
    final_probe = _probe(
        requested_mv=900,
        measured_mv=890.0,
        fps=170.0,
        power_w=240.0,
        temp_c=64.0,
    )
    final_probe.max_temperature_c = 68.0

    payload = build_auto_uv_fan_payload(final_probe=final_probe, probes=[final_probe])

    assert payload is not None
    curve = payload["fan"]["curve"]
    assert len(curve) <= int(payload["max_curve_points"])
    assert curve[0] == [45.0, 0.0]
    assert curve[1] == [60.0, 30.0]
    assert [80.0, 75.0] in curve
    assert curve[-1] == [90.0, 100.0]
    assert all(left[0] < right[0] for left, right in zip(curve, curve[1:]))
    assert all(left[1] <= right[1] for left, right in zip(curve, curve[1:]))
