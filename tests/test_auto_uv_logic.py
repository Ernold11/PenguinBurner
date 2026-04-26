from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path

import auto_uv.artifacts as auto_uv_artifacts
import auto_uv.candidate_sweep as candidate_sweep_module
from auto_uv.candidate_sweep import (
    _candidate_voltage_repeated,
    _current_candidate_target_mhz,
    _efficiency_stop_can_finish,
    _predicted_clock_floor_miss_reason,
    _run_candidate_sweep,
    _step_back_clock_bump_target,
)
from auto_uv.clock_bump import (
    _clock_bump_budget_pct,
    _format_clock_bump_budget,
    _clock_bump_needed_pct,
    _next_clock_bump_target_mhz,
)
from auto_uv.curve_planning import (
    _build_descended_plan,
    _choose_strictly_higher_clock_target,
    _choose_sustained_clock_target,
    _make_curve_candidate,
    _next_search_candidate_voltage_mv,
    _unsafe_min_search_voltage_mv,
    _validate_auto_uv_source_plan,
)
from auto_uv.fan_tuning import build_auto_uv_fan_payload
from auto_uv.final_verify import _choose_final_comparison_probe
from auto_uv.models import AutoUvError, AutoUvProbeSummary
from auto_uv.probe_metrics import _temperature_normalized_efficiency_delta
from auto_uv.probe_runner import _probe_phase_writes_crash_marker
from auto_uv.scan import (
    _build_voltage_scan_result,
    _clock_bump_budget_limit_from_unsafe_entries,
    _clock_bump_recovery_limit_from_unsafe_entries,
    _core_clock_below_floor,
    _final_failure_can_accept_budget_curve,
    _is_power_up_efficiency_down_regression,
    _probe_failure_should_mark_voltage_unsafe,
    _real_clock_adjusted_stable_curve,
    _target_core_clock_floor,
    _telemetry_sample_is_busy,
)
from auto_uv.tuning import AUTO_UV_METRIC_TUNING
from auto_uv.user_output import (
    log_user_candidate_result,
    log_user_readable_final_summary,
)
from stability.q2rtx.models import Q2RTXStabilityConfig


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


def test_default_auto_uv_clock_drop_is_ten_percent() -> None:
    assert AUTO_UV_METRIC_TUNING.max_core_clock_drop_pct == 10.0
    assert AUTO_UV_METRIC_TUNING.min_performance_core_clock_pct == 90.0


def _probe(
    *,
    requested_mv: int,
    measured_mv: float,
    fps: float,
    power_w: float,
    temp_c: float,
    core_clock_mhz: float = 2600.0,
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
        avg_core_clock_mhz=float(core_clock_mhz),
        efficiency_fps_per_w=float(fps) / float(power_w),
        efficiency_mhz_per_w=float(core_clock_mhz) / float(power_w),
        watts_per_mhz=float(power_w) / float(core_clock_mhz),
        used_companion_load=False,
        result_reason="passed",
        log_path=Path("synthetic.log"),
    )


def test_final_scan_result_compares_against_initial_discovery_not_flattened_probe() -> (
    None
):
    discovery = _probe(
        requested_mv=1020,
        measured_mv=1019.0,
        fps=161.0,
        power_w=329.8,
        temp_c=63.0,
        core_clock_mhz=2744.2,
    )
    flattened_baseline = _probe(
        requested_mv=1020,
        measured_mv=976.5,
        fps=156.9,
        power_w=305.5,
        temp_c=62.7,
        core_clock_mhz=2625.3,
    )
    final = _probe(
        requested_mv=910,
        measured_mv=898.0,
        fps=153.3,
        power_w=256.9,
        temp_c=61.7,
        core_clock_mhz=2441.9,
    )

    result = _build_voltage_scan_result(
        final_voltage_mv=910,
        final_lock_clock_mhz=2445,
        initial_probe=discovery,
        probe_history=[discovery, flattened_baseline, final],
        final_probe=final,
    )

    assert result.baseline_power_w == 329.8
    assert result.baseline_core_clock_mhz == 2744.2
    assert round(result.power_saved_w or 0.0, 1) == 72.9
    assert round(result.core_clock_drop_pct or 0.0, 2) == 11.02


def test_user_final_summary_reports_completed_long_verification() -> None:
    baseline = _probe(
        requested_mv=1020,
        measured_mv=1019.0,
        fps=161.0,
        power_w=329.8,
        temp_c=63.0,
        core_clock_mhz=2744.2,
    )
    final = _probe(
        requested_mv=910,
        measured_mv=898.0,
        fps=153.3,
        power_w=256.9,
        temp_c=61.7,
        core_clock_mhz=2441.9,
    )
    lines: list[str] = []

    log_user_readable_final_summary(
        lines.append,
        baseline_probe=baseline,
        final_probe=final,
        final_voltage_mv=910,
        final_lock_clock_mhz=2445,
        clock_drop_margin_pct=12.0,
        curve_path=Path("auto-uv-final-curve.json"),
        final_verification_status="completed 600s long check",
    )

    assert "Final verification: completed 600s long check" in lines
    assert any("| Metric" in line and "| Stock" in line for line in lines)
    assert any("Change vs stock" in line for line in lines)
    fps_row = next(line for line in lines if "| FPS" in line and "FPS/W" not in line)
    assert "161.0FPS" in fps_row
    assert "153.3FPS" in fps_row


def test_final_comparison_prefers_matching_short_scan_probe() -> None:
    stable = _probe(
        requested_mv=840,
        measured_mv=840.0,
        fps=145.4,
        power_w=225.5,
        temp_c=62.0,
        core_clock_mhz=2444.1,
    )
    final_long = _probe(
        requested_mv=840,
        measured_mv=840.0,
        fps=153.6,
        power_w=232.4,
        temp_c=63.0,
        core_clock_mhz=2445.5,
    )

    chosen = _choose_final_comparison_probe(
        stable_probe=stable,
        final_probe=final_long,
        final_voltage_mv=840,
        final_lock_clock_mhz=2700,
    )

    assert chosen is stable
    assert chosen.avg_fps == 145.4


def test_final_comparison_uses_long_probe_when_final_curve_changed() -> None:
    stable = _probe(
        requested_mv=840,
        measured_mv=840.0,
        fps=145.4,
        power_w=225.5,
        temp_c=62.0,
        core_clock_mhz=2444.1,
    )
    final_long = _probe(
        requested_mv=840,
        measured_mv=840.0,
        fps=148.0,
        power_w=232.4,
        temp_c=63.0,
        core_clock_mhz=2460.0,
    )

    chosen = _choose_final_comparison_probe(
        stable_probe=stable,
        final_probe=final_long,
        final_voltage_mv=840,
        final_lock_clock_mhz=2715,
    )

    assert chosen is final_long


def test_candidate_result_reports_initial_measured_clock_not_curve_target() -> None:
    initial = _probe(
        requested_mv=1020,
        measured_mv=1019.0,
        fps=161.0,
        power_w=329.8,
        temp_c=63.0,
        core_clock_mhz=2730.2,
    )
    initial.lock_clock_mhz = 3180
    previous = _probe(
        requested_mv=970,
        measured_mv=912.0,
        fps=157.0,
        power_w=300.0,
        temp_c=62.0,
        core_clock_mhz=2608.0,
    )
    previous.lock_clock_mhz = 2610
    candidate = _probe(
        requested_mv=960,
        measured_mv=910.0,
        fps=156.0,
        power_w=294.0,
        temp_c=62.0,
        core_clock_mhz=2565.0,
    )
    candidate.lock_clock_mhz = 2565
    lines: list[str] = []

    log_user_candidate_result(
        lines.append,
        attempt=4,
        decision="accepted",
        reason="synthetic",
        initial_probe=initial,
        previous_probe=previous,
        candidate_probe=candidate,
    )

    assert "This Step Compared With Stock And Previous Stable" in lines
    assert any("| Metric" in line and "| Stock" in line for line in lines)
    assert any("Change vs stock" in line for line in lines)
    target_clock_row = next(line for line in lines if "| Target clock" in line)
    assert "2730MHz" in target_clock_row
    assert "3180MHz" not in target_clock_row
    assert "-165MHz" in target_clock_row
    fps_row = next(line for line in lines if "| FPS" in line and "FPS/W" not in line)
    assert "161.0FPS" in fps_row
    assert "157.0FPS" in fps_row
    assert "156.0FPS" in fps_row


def test_descended_plan_smooths_bins_below_candidate_voltage() -> None:
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
    source_by_voltage = {item["voltage_mv"]: item for item in source_plan}
    assert below[0]["target_mhz"] == source_plan[0]["target_mhz"]
    assert all(
        int(item["target_mhz"])
        >= int(source_by_voltage[item["voltage_mv"]]["target_mhz"])
        for item in below
    )
    assert (
        below[-1]["target_mhz"]
        > source_by_voltage[below[-1]["voltage_mv"]]["target_mhz"]
    )
    assert below[-1]["target_mhz"] < 2500
    assert all(int(item["target_mhz"]) == 2500 for item in at_or_above)


def test_descended_plan_does_not_lift_far_lower_voltage_bins_to_target() -> None:
    source_plan = [
        {
            "index": index,
            "voltage_mv": voltage_mv,
            "base_mhz": 1000 + index * 20,
            "target_mhz": 1000 + index * 20,
            "new_offset_mhz": 0,
        }
        for index, voltage_mv in enumerate([840, 865, 950, 960, 975, 985, 1000])
    ]

    descended = _build_descended_plan(
        source_plan,
        lock_clock_mhz=2715,
        candidate_voltage_mv=1000,
    )
    by_voltage = {int(item["voltage_mv"]): item for item in descended}
    source_by_voltage = {int(item["voltage_mv"]): item for item in source_plan}

    assert by_voltage[840]["target_mhz"] == source_by_voltage[840]["target_mhz"]
    assert by_voltage[865]["target_mhz"] == source_by_voltage[865]["target_mhz"]
    assert by_voltage[975]["target_mhz"] > source_by_voltage[975]["target_mhz"]
    assert by_voltage[975]["target_mhz"] < 2715
    assert by_voltage[1000]["target_mhz"] == 2715


def test_descended_plan_keeps_bins_below_candidate_below_plateau() -> None:
    source_plan = [
        {
            "index": index,
            "voltage_mv": voltage_mv,
            "base_mhz": target_mhz,
            "target_mhz": target_mhz,
            "new_offset_mhz": 0,
        }
        for index, (voltage_mv, target_mhz) in enumerate(
            [
                (985, 2685),
                (990, 2700),
                (995, 2715),
                (1000, 2745),
                (1010, 2745),
                (1020, 2745),
            ]
        )
    ]

    descended = _build_descended_plan(
        source_plan,
        lock_clock_mhz=2745,
        candidate_voltage_mv=1020,
    )
    by_voltage = {int(item["voltage_mv"]): item for item in descended}

    assert by_voltage[1000]["target_mhz"] == 2730
    assert by_voltage[1010]["target_mhz"] == 2730
    assert by_voltage[1020]["target_mhz"] == 2745


def test_curve_candidate_keeps_lower_bins_two_clock_steps_below_plateau() -> None:
    source_plan = [
        {
            "index": index,
            "voltage_mv": voltage_mv,
            "base_mhz": target_mhz,
            "target_mhz": target_mhz,
            "new_offset_mhz": 0,
        }
        for index, (voltage_mv, target_mhz) in enumerate(
            [
                (900, 2370),
                (910, 2400),
                (915, 2415),
                (940, 2430),
                (970, 2610),
            ]
        )
    ]

    candidate = _make_curve_candidate(
        source_plan,
        candidate_voltage_mv=970,
        target_clock_mhz=2415,
        label="test",
    )
    by_voltage = {int(item["voltage_mv"]): item for item in candidate.plan}

    assert by_voltage[940]["target_mhz"] <= 2385
    assert by_voltage[970]["target_mhz"] == 2415


def test_candidate_target_follows_source_vf_curve_downward() -> None:
    source_plan = [
        {
            "index": index,
            "voltage_mv": voltage_mv,
            "base_mhz": target_mhz,
            "target_mhz": target_mhz,
            "new_offset_mhz": 0,
        }
        for index, (voltage_mv, target_mhz) in enumerate(
            [
                (900, 2520),
                (925, 2580),
                (950, 2640),
                (975, 2640),
            ]
        )
    ]

    assert (
        _current_candidate_target_mhz(
            source_plan,
            stable_lock_clock_mhz=2640,
            candidate_voltage_mv=925,
            clock_bump_last_target_mhz=None,
        )
        == 2580
    )


def test_candidate_clock_predictor_flags_next_step_below_floor() -> None:
    history = [
        _probe(
            requested_mv=950,
            measured_mv=940.0,
            fps=160.0,
            power_w=270.0,
            temp_c=62.0,
            core_clock_mhz=2640.0,
        ),
        _probe(
            requested_mv=925,
            measured_mv=915.0,
            fps=158.0,
            power_w=255.0,
            temp_c=61.0,
            core_clock_mhz=2580.0,
        ),
    ]

    reason = _predicted_clock_floor_miss_reason(
        history,
        candidate_voltage_mv=900,
        floor_mhz=2535.0,
    )

    assert reason is not None
    assert "predicted-core_clock" in reason
    assert "current=2520.0MHz" in reason
    assert "floor=2535.0MHz" in reason


def test_candidate_clock_predictor_does_not_flag_when_prediction_stays_above_floor() -> (
    None
):
    history = [
        _probe(
            requested_mv=950,
            measured_mv=940.0,
            fps=160.0,
            power_w=270.0,
            temp_c=62.0,
            core_clock_mhz=2640.0,
        ),
        _probe(
            requested_mv=925,
            measured_mv=915.0,
            fps=158.0,
            power_w=255.0,
            temp_c=61.0,
            core_clock_mhz=2610.0,
        ),
    ]

    assert (
        _predicted_clock_floor_miss_reason(
            history,
            candidate_voltage_mv=900,
            floor_mhz=2535.0,
        )
        is None
    )


def test_strict_clock_bump_target_never_reuses_current_clock() -> None:
    source_plan = [
        {
            "index": index,
            "voltage_mv": voltage_mv,
            "base_mhz": target_mhz,
            "target_mhz": target_mhz,
            "new_offset_mhz": 0,
        }
        for index, (voltage_mv, target_mhz) in enumerate(
            [
                (900, 2370),
                (910, 2400),
                (940, 2415),
                (970, 2415),
                (1000, 2730),
            ]
        )
    ]

    bumped = _choose_strictly_higher_clock_target(
        source_plan,
        current_clock_mhz=2415,
        desired_clock_mhz=2415 * 1.02,
        cap_clock_mhz=2730,
    )
    capped = _choose_strictly_higher_clock_target(
        source_plan,
        current_clock_mhz=2730,
        desired_clock_mhz=2730 * 1.02,
        cap_clock_mhz=2730,
    )

    assert bumped is not None
    assert bumped > 2415
    assert capped is None


def test_strict_clock_bump_target_never_exceeds_initial_probe_clock_cap() -> None:
    source_plan = [
        {
            "index": index,
            "voltage_mv": voltage_mv,
            "base_mhz": target_mhz,
            "target_mhz": target_mhz,
            "new_offset_mhz": 0,
        }
        for index, (voltage_mv, target_mhz) in enumerate(
            [
                (900, 2700),
                (925, 2715),
                (950, 2730),
                (975, 2745),
                (1000, 2760),
            ]
        )
    ]

    bumped = _choose_strictly_higher_clock_target(
        source_plan,
        current_clock_mhz=2700,
        desired_clock_mhz=2700 * 1.02,
        cap_clock_mhz=2744.2,
    )

    assert bumped == 2730
    assert bumped <= 2744.2


def test_clock_bump_budget_is_derived_from_clock_drop_ratio() -> None:
    assert (
        _clock_bump_budget_pct(max_clock_drop_pct=12.0, bump_budget_ratio=0.5)
        == 6.0
    )
    assert (
        _clock_bump_budget_pct(max_clock_drop_pct=12.0, bump_budget_ratio=0.75)
        == 9.0
    )
    assert (
        _clock_bump_budget_pct(max_clock_drop_pct=12.0, bump_budget_ratio=0.25)
        == 3.0
    )


def test_clock_bump_budget_ratio_is_clamped_to_drop_budget() -> None:
    assert (
        _clock_bump_budget_pct(
            max_clock_drop_pct=12.0,
            bump_budget_ratio=1.5,
        )
        == 12.0
    )
    assert (
        _clock_bump_budget_pct(
            max_clock_drop_pct=12.0,
            bump_budget_ratio=-1.0,
        )
        == 0.0
    )


def test_clock_bump_budget_format_reports_remaining_budget() -> None:
    assert (
        _format_clock_bump_budget(used_pct=2.25, limit_pct=6.0)
        == "overclocking-budget=2.25/6.00%"
    )
    assert (
        _format_clock_bump_budget(used_pct=7.0, limit_pct=6.0)
        == "overclocking-budget=7.00/6.00%"
    )


def test_efficiency_stop_finishes_when_bump_budget_is_exhausted() -> None:
    assert _efficiency_stop_can_finish(
        efficiency_stop_candidate=True,
        efficiency_stop_allowed=True,
        pending_efficiency_stop_curve={"voltage_mv": 925},
        non_improving_efficiency_streak=2,
        efficiency_stop_streak=1,
        clock_bump_budget_used_pct=6.0,
        clock_bump_budget_limit_pct=6.0,
    )


def test_efficiency_stop_waits_when_bump_budget_remains() -> None:
    assert not _efficiency_stop_can_finish(
        efficiency_stop_candidate=True,
        efficiency_stop_allowed=True,
        pending_efficiency_stop_curve={"voltage_mv": 925},
        non_improving_efficiency_streak=2,
        efficiency_stop_streak=1,
        clock_bump_budget_used_pct=3.0,
        clock_bump_budget_limit_pct=6.0,
    )


def test_efficiency_stop_does_not_require_reaching_clock_drop_floor() -> None:
    assert _efficiency_stop_can_finish(
        efficiency_stop_candidate=True,
        efficiency_stop_allowed=True,
        pending_efficiency_stop_curve={"voltage_mv": 925},
        non_improving_efficiency_streak=2,
        efficiency_stop_streak=1,
        clock_bump_budget_used_pct=6.0,
        clock_bump_budget_limit_pct=6.0,
    )


def test_candidate_voltage_repeat_guard_detects_duplicate_outer_step() -> None:
    seen: set[int] = set()

    assert not _candidate_voltage_repeated(seen, 950)
    assert not _candidate_voltage_repeated(seen, 925)
    assert _candidate_voltage_repeated(seen, 950)


def test_clock_bump_target_steps_back_one_grid_point_after_hard_failure() -> None:
    source_plan = [
        {
            "index": index,
            "voltage_mv": voltage_mv,
            "base_mhz": target_mhz,
            "target_mhz": target_mhz,
            "new_offset_mhz": 0,
        }
        for index, (voltage_mv, target_mhz) in enumerate(
            [
                (875, 2520),
                (900, 2535),
                (925, 2550),
            ]
        )
    ]

    assert (
        _step_back_clock_bump_target(
            source_plan,
            current_target_mhz=2550,
            clock_bump_last_target_mhz=2550,
        )
        == 2535
    )


def test_clock_bump_target_is_not_backed_off_without_active_bump() -> None:
    assert (
        _step_back_clock_bump_target(
            _plan(),
            current_target_mhz=2550,
            clock_bump_last_target_mhz=None,
        )
        == 2550
    )


def test_candidate_sweep_stops_if_candidate_selector_repeats_voltage(
    monkeypatch,
) -> None:
    source_plan = _plan()
    discovery = _probe(
        requested_mv=1000,
        measured_mv=1000.0,
        fps=100.0,
        power_w=200.0,
        temp_c=60.0,
        core_clock_mhz=2700.0,
    )
    probe_history: list[AutoUvProbeSummary] = []
    logs: list[str] = []
    probe_calls: list[int] = []

    class _Reader:
        def refresh_points(self) -> None:
            return None

    class _NvmlSession:
        def read_live_voltage_mv(self) -> int:
            return 975

    def _probe_voltage_candidate(**kwargs):
        candidate_voltage_mv = int(kwargs["candidate_voltage_mv"])
        probe_calls.append(candidate_voltage_mv)
        return (
            _probe(
                requested_mv=candidate_voltage_mv,
                measured_mv=float(candidate_voltage_mv),
                fps=100.0,
                power_w=200.0,
                temp_c=60.0,
                core_clock_mhz=2650.0,
            ),
            SimpleNamespace(success=True, reason="passed"),
        )

    monkeypatch.setattr(candidate_sweep_module, "apply_plan", lambda *_, **__: None)
    monkeypatch.setattr(
        candidate_sweep_module,
        "_write_latest_verified_uv_result",
        lambda *_, **__: None,
    )
    monkeypatch.setattr(
        candidate_sweep_module,
        "_next_search_candidate_voltage_mv",
        lambda **_: 975,
    )

    result = _run_candidate_sweep(
        probe_voltage_candidate=_probe_voltage_candidate,
        probe_stabilization_search=lambda **_: (None, None, None),
        describe_guardrails=lambda *_args, **_kwargs: "guardrails",
        latest_reference_voltage_mv=lambda stable, fallback: (
            stable[-1].avg_voltage_mv if stable else fallback
        ),
        log=logs.append,
        reader=_Reader(),
        flattened_plan=source_plan,
        start_voltage_mv=1000,
        stable_plan=source_plan,
        stable_voltage_mv=1000,
        stable_lock_clock_mhz=2700,
        stable_probe=discovery,
        stable_history=[discovery],
        probe_history=probe_history,
        first_candidate_voltage_mv=975,
        discovery_summary=discovery,
        lock_clock_mhz=2700,
        q2rtx_config=Q2RTXStabilityConfig(duration_s=1),
        measured_clock_mhz=2700.0,
        nvml_session=_NvmlSession(),
        translated_gpu_policy={},
        runtime_default_plan=source_plan,
        clock_ceiling=None,
        source_result={"plan": source_plan},
        min_performance_core_clock_pct=80.0,
        min_search_voltage_mv=800,
        preserve_vanilla_below_mv=None,
        min_efficiency_stop_voltage_drop_pct=10.0,
        efficiency_stop_streak=0,
        clock_bump_budget_limit_pct=0.0,
    )

    assert probe_calls == [975]
    assert result["stable_voltage_mv"] == 975
    assert any("selected twice" in line for line in logs)


def test_clock_bump_need_uses_measured_shortfall_plus_one_step() -> None:
    needed_pct = _clock_bump_needed_pct(
        current_target_clock_mhz=2700,
        reason="telemetry-live-core_clock-avg current=2670.0MHz floor=2685.0MHz",
    )

    assert round(needed_pct, 3) == round((30.0 / 2700.0) * 100.0, 3)


def test_zero_clock_bump_budget_disables_next_bump_target() -> None:
    source_plan = [
        {
            "index": index,
            "voltage_mv": voltage_mv,
            "base_mhz": target_mhz,
            "target_mhz": target_mhz,
            "new_offset_mhz": 0,
        }
        for index, (voltage_mv, target_mhz) in enumerate(
            [
                (900, 2700),
                (925, 2715),
                (950, 2730),
            ]
        )
    ]

    assert (
        _next_clock_bump_target_mhz(
            source_plan,
            current_clock_mhz=2700,
            cap_clock_mhz=2730.0,
            remaining_budget_pct=0.0,
        )
        is None
    )


def test_sustained_clock_target_never_rounds_above_measured_clock() -> None:
    source_plan = [
        {
            "index": index,
            "voltage_mv": voltage_mv,
            "base_mhz": target_mhz,
            "target_mhz": target_mhz,
            "new_offset_mhz": 0,
        }
        for index, (voltage_mv, target_mhz) in enumerate(
            [
                (900, 2700),
                (925, 2715),
                (950, 2730),
                (975, 2745),
                (1000, 2760),
            ]
        )
    ]

    target = _choose_sustained_clock_target(source_plan, 2744.2)

    assert target == 2730
    assert target <= 2744.2


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


def test_accepted_stable_curve_follows_real_measured_clock() -> None:
    source_plan = [
        {
            "index": index,
            "voltage_mv": voltage_mv,
            "base_mhz": target_mhz,
            "target_mhz": target_mhz,
            "new_offset_mhz": 0,
        }
        for index, (voltage_mv, target_mhz) in enumerate(
            [
                (850, 2100),
                (875, 2355),
                (900, 2520),
                (925, 2640),
                (950, 2700),
            ]
        )
    ]
    probe = _probe(
        requested_mv=900,
        measured_mv=890.0,
        fps=160.0,
        power_w=260.0,
        temp_c=62.0,
    )
    probe.avg_core_clock_mhz = 2488.0

    adjusted_plan, adjusted_lock_mhz = _real_clock_adjusted_stable_curve(
        source_plan,
        candidate_voltage_mv=900,
        previous_lock_clock_mhz=2700,
        probe=probe,
    )

    assert adjusted_lock_mhz == 2475
    assert all(
        int(item["target_mhz"]) == 2475
        for item in adjusted_plan
        if int(item["voltage_mv"]) >= 900
    )


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


def test_final_budget_curve_can_accept_only_clock_floor_failures() -> None:
    assert (
        _final_failure_can_accept_budget_curve(
            "telemetry-live-core_clock-avg current=2383.7MHz floor=2389.2MHz tolerance=5.0MHz"
        )
        is True
    )
    assert (
        _final_failure_can_accept_budget_curve(
            "core_clock-regression current=2383.7MHz baseline=2715.0MHz floor=2389.2MHz"
        )
        is True
    )
    assert _final_failure_can_accept_budget_curve("nvidia-xid-detected") is False


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


def test_stale_auto_uv_probe_marker_records_abrupt_previous_end(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(auto_uv_artifacts, "default_user_config_dir", lambda: tmp_path)

    marker_path = auto_uv_artifacts._write_uv_probe_in_progress(
        phase="candidate",
        candidate_voltage_mv=940,
        lock_clock_mhz=2430,
        log_context="candidate=940mV target=2430MHz",
        details={"clock_bump_attempt": 3, "clock_bump_limit": 3},
    )

    consumed = auto_uv_artifacts._consume_interrupted_uv_probe_marker()

    assert consumed is not None
    blacklist_path, entry = consumed
    assert not marker_path.exists()
    assert blacklist_path == tmp_path / "uv-result" / "auto-uv-unsafe-voltages.json"
    assert entry["reason"] == "previous-run-abruptly-ended"
    assert entry["candidate_voltage_mv"] == 940
    assert entry["lock_clock_mhz"] == 2430
    assert entry["phase"] == "candidate"
    assert entry["details"]["marker_pid"] is not None
    assert entry["details"]["marker_details"]["clock_bump_attempt"] == 3
    assert "clean Ctrl-C/SIGTERM" in entry["details"]["classification"]


def test_abrupt_candidate_recovery_marker_caps_future_bumps_to_n_minus_one() -> None:
    unsafe_entries = [
        {
            "reason": "previous-run-abruptly-ended",
            "phase": "candidate-recovery",
            "candidate_voltage_mv": 920,
            "lock_clock_mhz": 2500,
            "details": {
                "marker_details": {
                    "clock_bump_attempt": 3,
                    "clock_bump_limit": 3,
                }
            },
        }
    ]

    assert _clock_bump_recovery_limit_from_unsafe_entries(unsafe_entries, 3) == 2
    assert _clock_bump_recovery_limit_from_unsafe_entries(unsafe_entries, 5) == 2


def test_abrupt_budgeted_recovery_marker_caps_future_budget_to_before_failed_bump() -> (
    None
):
    unsafe_entries = [
        {
            "reason": "previous-run-abruptly-ended",
            "phase": "candidate-recovery",
            "candidate_voltage_mv": 920,
            "lock_clock_mhz": 2500,
            "details": {
                "marker_details": {
                    "clock_bump_budget_used_before_pct": 2.5,
                    "clock_bump_budget_used_after_pct": 4.0,
                    "clock_bump_budget_limit_pct": 6.0,
                }
            },
        }
    ]

    assert _clock_bump_budget_limit_from_unsafe_entries(unsafe_entries, 6.0) == 2.5


def test_final_recovery_probe_phase_writes_crash_marker() -> None:
    assert _probe_phase_writes_crash_marker("final-recovery") is True


def test_abrupt_first_candidate_recovery_marker_disables_future_bumps() -> None:
    unsafe_entries = [
        {
            "reason": "previous-run-abruptly-ended",
            "phase": "candidate-recovery",
            "details": {"marker_details": {"clock_bump_attempt": 1}},
        }
    ]

    assert _clock_bump_recovery_limit_from_unsafe_entries(unsafe_entries, 3) == 0


def test_non_probing_auto_uv_marker_is_not_blacklisted(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(auto_uv_artifacts, "default_user_config_dir", lambda: tmp_path)
    marker_path = tmp_path / "uv-result" / "auto-uv-probe-in-progress.json"
    marker_path.parent.mkdir(parents=True)
    marker_path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "state": "clean-shutdown",
                "candidate_voltage_mv": 940,
                "lock_clock_mhz": 2430,
            }
        )
    )

    assert auto_uv_artifacts._consume_interrupted_uv_probe_marker() is None
    assert not (tmp_path / "uv-result" / "auto-uv-unsafe-voltages.json").exists()


def test_malformed_unsafe_entries_do_not_block_new_unsafe_record(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(auto_uv_artifacts, "default_user_config_dir", lambda: tmp_path)
    blacklist_path = tmp_path / "uv-result" / "auto-uv-unsafe-voltages.json"
    blacklist_path.parent.mkdir(parents=True)
    blacklist_path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "entries": [
                    {
                        "candidate_voltage_mv": "not-a-number",
                        "lock_clock_mhz": "also-bad",
                        "reason": "previous-run-abruptly-ended",
                    }
                ],
            }
        )
    )

    path, entry = auto_uv_artifacts._record_unsafe_uv_voltage(
        candidate_voltage_mv=940,
        lock_clock_mhz=2430,
        reason="stability-probe-failed",
    )

    payload = json.loads(path.read_text())
    assert entry["candidate_voltage_mv"] == 940
    assert payload["entries"][-1]["lock_clock_mhz"] == 2430


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
