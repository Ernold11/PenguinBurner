from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path

import pytest

import auto_uv.artifact_paths as auto_uv_artifact_paths
import auto_uv.artifacts as auto_uv_artifacts
import auto_uv.profiles as auto_uv_profiles
import auto_uv.scan as auto_uv_scan
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
from auto_uv.fan_tuning import build_auto_uv_fan_payload, write_auto_uv_fan_payload
from auto_uv.final_verify import _choose_final_comparison_probe
from auto_uv.models import AutoUvError, AutoUvFinalChoiceDiscarded, AutoUvProbeSummary
from auto_uv.probe_metrics import _temperature_normalized_efficiency_delta
from auto_uv.probe_runner import _probe_phase_writes_crash_marker
from auto_uv.scan import (
    _build_voltage_scan_result,
    _clock_bump_budget_limit_from_unsafe_entries,
    _clock_bump_recovery_limit_from_unsafe_entries,
    _coerce_final_choice_duration_s,
    _core_clock_below_floor,
    _choose_final_verification_candidate,
    _final_failure_can_accept_budget_curve,
    _is_power_up_efficiency_down_regression,
    _probe_failure_should_mark_voltage_unsafe,
    _real_clock_adjusted_stable_curve,
    _short_verification_duration_s,
    _target_core_clock_floor,
    _telemetry_sample_is_busy,
)
from auto_uv.tuning import AUTO_UV_DEFAULTS, AUTO_UV_METRIC_TUNING
from auto_uv.user_output import (
    format_user_duration,
    log_user_candidate_result,
    log_user_readable_final_summary,
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


def test_default_auto_uv_clock_drop_is_ten_percent() -> None:
    assert AUTO_UV_DEFAULTS.max_drop_pct == 16.0
    assert AUTO_UV_METRIC_TUNING.max_core_clock_drop_pct == 10.0
    assert AUTO_UV_METRIC_TUNING.min_performance_core_clock_pct == 90.0
    assert AUTO_UV_DEFAULTS.clock_bump_budget_ratio == 0.5
    assert (
        _clock_bump_budget_pct(
            max_clock_drop_pct=AUTO_UV_METRIC_TUNING.max_core_clock_drop_pct,
            bump_budget_ratio=AUTO_UV_DEFAULTS.clock_bump_budget_ratio,
        )
        == 5.0
    )


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
    assert any("| Metric" in line and "| Base" in line for line in lines)
    assert any("Change vs base" in line for line in lines)
    target_clock_row = next(line for line in lines if "| Target clock" in line)
    measured_clock_row = next(line for line in lines if "| Measured core clock" in line)
    assert "2700.0MHz" in target_clock_row
    assert "2445.0MHz" in target_clock_row
    assert "2744.2MHz" in measured_clock_row
    assert "2441.9MHz" in measured_clock_row
    fps_row = next(line for line in lines if "| FPS" in line and "FPS/W" not in line)
    assert "161.0FPS" in fps_row
    assert "153.3FPS" in fps_row


def test_user_duration_format_uses_simple_time_units() -> None:
    assert format_user_duration(45) == "45 sec"
    assert format_user_duration(90) == "1 min 30 sec"
    assert format_user_duration(600) == "10 min"
    assert format_user_duration(3900) == "1 h 5 min"


def test_final_choice_duration_is_clamped_to_short_probe_and_one_hour() -> None:
    assert (
        _coerce_final_choice_duration_s(30, default_s=600, min_s=90, max_s=3600)
        == 90
    )
    assert (
        _coerce_final_choice_duration_s(9999, default_s=600, min_s=90, max_s=3600)
        == 3600
    )
    assert (
        _coerce_final_choice_duration_s(None, default_s=30, min_s=90, max_s=3600)
        == 90
    )


def test_final_choice_uses_only_current_run_candidates(tmp_path, monkeypatch) -> None:
    source_plan = _plan()
    current_stable_plan = _build_descended_plan(
        source_plan,
        lock_clock_mhz=2700,
        candidate_voltage_mv=900,
    )
    current_probe = _probe(
        requested_mv=900,
        measured_mv=890.0,
        fps=150.0,
        power_w=200.0,
        core_clock_mhz=2600.0,
        temp_c=55.0,
    )
    older_probe = _probe(
        requested_mv=840,
        measured_mv=835.0,
        fps=1000.0,
        power_w=1.0,
        core_clock_mhz=2700.0,
        temp_c=55.0,
    )
    older_plan = _build_descended_plan(
        source_plan,
        lock_clock_mhz=2700,
        candidate_voltage_mv=900,
    )
    stale_candidate = auto_uv_artifacts._uv_candidate_payload(
        plan=older_plan,
        lock_clock_mhz=2700,
        voltage_mv=840,
        probe=older_probe,
        reason="latest-verified",
        label="passed-short-probe",
    )
    request_path = tmp_path / "auto-uv-final-choice-request.json"
    response_path = tmp_path / "auto-uv-final-choice.json"
    captured_request = {}

    monkeypatch.setattr(auto_uv_scan, "_final_choice_request_path", lambda: request_path)
    monkeypatch.setattr(auto_uv_scan, "_final_choice_response_path", lambda: response_path)
    monkeypatch.setattr(
        auto_uv_scan,
        "_read_verified_uv_candidates",
        lambda: (_ for _ in ()).throw(
            AssertionError("final choice must not read persisted verified candidates")
        ),
        raising=False,
    )

    def fake_safe_json_write(path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        if Path(path) == request_path:
            captured_request.update(payload)
            response_path.write_text("{}", encoding="utf-8")
        return path

    monkeypatch.setattr(auto_uv_scan, "_safe_json_write", fake_safe_json_write)

    selected_plan, selected_voltage_mv, selected_clock_mhz, selected_probe, _duration = (
        _choose_final_verification_candidate(
            log=lambda _message: None,
            event_callback=None,
            stable_plan=current_stable_plan,
            stable_voltage_mv=900,
            stable_lock_clock_mhz=2700,
            stable_probe=current_probe,
            stable_history=[current_probe],
            source_plan=source_plan,
            final_verification_duration_s=600,
            initial_target_voltage_mv=1000,
            short_probe_base_duration_s=30,
        )
    )

    candidate_ids = {
        str(candidate.get("candidate_id", ""))
        for candidate in captured_request["candidates"]
    }
    assert "840mv-2700mhz" not in candidate_ids
    assert stale_candidate["candidate_id"] not in candidate_ids
    assert captured_request["default_candidate_id"] == "900mv-2700mhz"
    assert selected_voltage_mv == 900
    assert selected_clock_mhz == 2700
    assert selected_probe is current_probe
    assert selected_plan == current_stable_plan


def test_final_choice_discard_response_skips_selection(tmp_path, monkeypatch) -> None:
    source_plan = _plan()
    current_stable_plan = _build_descended_plan(
        source_plan,
        lock_clock_mhz=2700,
        candidate_voltage_mv=900,
    )
    current_probe = _probe(
        requested_mv=900,
        measured_mv=890.0,
        fps=150.0,
        power_w=200.0,
        core_clock_mhz=2600.0,
        temp_c=55.0,
    )
    request_path = tmp_path / "auto-uv-final-choice-request.json"
    response_path = tmp_path / "auto-uv-final-choice.json"
    events = []

    monkeypatch.setattr(auto_uv_scan, "_final_choice_request_path", lambda: request_path)
    monkeypatch.setattr(auto_uv_scan, "_final_choice_response_path", lambda: response_path)

    def fake_safe_json_write(path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        if Path(path) == request_path:
            response_path.write_text(
                json.dumps({"action": "discard"}) + "\n",
                encoding="utf-8",
            )
        return path

    monkeypatch.setattr(auto_uv_scan, "_safe_json_write", fake_safe_json_write)

    with pytest.raises(AutoUvFinalChoiceDiscarded):
        _choose_final_verification_candidate(
            log=lambda _message: None,
            event_callback=lambda event, payload: events.append((event, payload)),
            stable_plan=current_stable_plan,
            stable_voltage_mv=900,
            stable_lock_clock_mhz=2700,
            stable_probe=current_probe,
            stable_history=[current_probe],
            source_plan=source_plan,
            final_verification_duration_s=600,
            initial_target_voltage_mv=1000,
            short_probe_base_duration_s=30,
        )

    assert ("final_choice_discarded", {"reason": "user-discarded"}) in events


def test_short_verification_duration_tracks_voltage_band() -> None:
    assert (
        _short_verification_duration_s(
            initial_target_voltage_mv=1000,
            candidate_voltage_mv=960,
        )
        == 30
    )
    assert (
        _short_verification_duration_s(
            initial_target_voltage_mv=1000,
            candidate_voltage_mv=920,
        )
        == 60
    )
    assert (
        _short_verification_duration_s(
            initial_target_voltage_mv=1000,
            candidate_voltage_mv=880,
        )
        == 90
    )


def test_short_verification_duration_scales_from_base_duration() -> None:
    assert (
        _short_verification_duration_s(
            initial_target_voltage_mv=1000,
            candidate_voltage_mv=960,
            base_duration_s=40,
        )
        == 40
    )
    assert (
        _short_verification_duration_s(
            initial_target_voltage_mv=1000,
            candidate_voltage_mv=920,
            base_duration_s=40,
        )
        == 80
    )
    assert (
        _short_verification_duration_s(
            initial_target_voltage_mv=1000,
            candidate_voltage_mv=880,
            base_duration_s=40,
        )
        == 120
    )


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

    assert "This Step Compared With Base And Previous Stable" in lines
    assert any("| Metric" in line and "| Base" in line for line in lines)
    assert any("Change vs base" in line for line in lines)
    target_clock_row = next(line for line in lines if "| Target clock" in line)
    measured_clock_row = next(line for line in lines if "| Measured core clock" in line)
    assert "3180MHz" in target_clock_row
    assert "2565MHz" in target_clock_row
    assert "2730.2MHz" in measured_clock_row
    assert "2565.0MHz" in measured_clock_row
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


def test_clock_bump_target_rejects_snapped_bin_over_budget() -> None:
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
            ]
        )
    ]

    assert (
        _next_clock_bump_target_mhz(
            source_plan,
            current_clock_mhz=2700,
            cap_clock_mhz=2745.0,
            remaining_budget_pct=0.01,
            fallback_bump_pct=0.01,
        )
        is None
    )
    assert (
        _next_clock_bump_target_mhz(
            source_plan,
            current_clock_mhz=2700,
            cap_clock_mhz=2745.0,
            remaining_budget_pct=0.6,
            fallback_bump_pct=0.01,
        )
        == 2715
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
        preserve_base_below_mv=None,
        min_search_voltage_mv=next_safe,
        failed_floor_voltage_mv=unsafe_floor,
    )

    assert unsafe_floor == 875
    assert next_safe == 900
    assert candidate == 900
    assert candidate > unsafe_floor


def test_candidate_search_does_not_jump_from_900_to_far_lower_bin() -> None:
    source_plan = [
        {
            "index": index,
            "voltage_mv": voltage_mv,
            "base_mhz": 2000 + index * 30,
            "target_mhz": 2000 + index * 30,
            "new_offset_mhz": 0,
        }
        for index, voltage_mv in enumerate(
            [840, 845, 850, 860, 865, 870, 875, 885, 890, 895, 900]
        )
    ]

    candidate = _next_search_candidate_voltage_mv(
        plan=source_plan,
        start_voltage_mv=1025,
        stable_voltage_mv=900,
        reference_actual_voltage_mv=998.0,
        preserve_base_below_mv=None,
        min_search_voltage_mv=840,
    )

    assert candidate == 895


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


def test_accepted_stable_curve_follows_loaded_voltage_and_clock() -> None:
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

    assert adjusted_lock_mhz == 2505
    assert all(
        int(item["target_mhz"]) == 2505
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
        is False
    )
    assert (
        _probe_failure_should_mark_voltage_unsafe(
            "telemetry-live-core_clock-avg current=2457.2MHz floor=2475.9MHz tolerance=5.0MHz"
        )
        is False
    )
    assert (
        _probe_failure_should_mark_voltage_unsafe("cuda-bruteforce-failed exit=-15")
        is False
    )
    assert (
        _probe_failure_should_mark_voltage_unsafe("cuda-bruteforce-failed exit=-6")
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
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        auto_uv_artifact_paths,
        "default_user_config_dir",
        lambda: tmp_path,
    )
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


def test_controlled_clock_floor_abort_does_not_cap_future_voltage_search() -> None:
    unsafe_floor, next_safe = _unsafe_min_search_voltage_mv(
        plan=_plan(),
        start_voltage_mv=1025,
        unsafe_entries=[
            {
                "reason": "stability-probe-failed",
                "candidate_voltage_mv": 895,
                "lock_clock_mhz": 2580,
                "details": {
                    "result_reason": (
                        "telemetry-live-core_clock-avg current=2457.2MHz "
                        "floor=2475.9MHz tolerance=5.0MHz"
                    ),
                    "shutdown_mode": (
                        "telemetry-live-core_clock-avg current=2457.2MHz "
                        "floor=2475.9MHz tolerance=5.0MHz"
                    ),
                    "process_exit_code": -6,
                    "workload_kind": "timedemo",
                },
            }
        ],
    )

    assert unsafe_floor is None
    assert next_safe is None


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
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        auto_uv_artifact_paths,
        "default_user_config_dir",
        lambda: tmp_path,
    )
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
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        auto_uv_artifact_paths,
        "default_user_config_dir",
        lambda: tmp_path,
    )
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


def test_legacy_clock_floor_unsafe_entries_are_ignored(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        auto_uv_artifacts,
        "_unsafe_voltage_blacklist_path",
        lambda: tmp_path / "auto-uv-unsafe-voltages.json",
    )
    blacklist_path = auto_uv_artifacts._unsafe_voltage_blacklist_path()
    blacklist_path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "entries": [
                    {
                        "reason": "stability-probe-failed",
                        "candidate_voltage_mv": 895,
                        "lock_clock_mhz": 2580,
                        "details": {
                            "result_reason": (
                                "telemetry-live-core_clock-avg current=2457.2MHz "
                                "floor=2475.9MHz tolerance=5.0MHz"
                            ),
                            "process_exit_code": -6,
                            "workload_kind": "timedemo",
                        },
                    },
                    {
                        "reason": "previous-run-abruptly-ended",
                        "candidate_voltage_mv": 875,
                        "lock_clock_mhz": 2535,
                    },
                    {
                        "reason": "stability-probe-failed",
                        "candidate_voltage_mv": 990,
                        "lock_clock_mhz": 2640,
                        "details": {
                            "result_reason": "cuda-bruteforce-failed exit=-15",
                            "process_exit_code": 0,
                            "workload_kind": "timedemo",
                        },
                    },
                ],
            }
        )
    )

    entries = auto_uv_artifacts._load_uv_unsafe_voltage_entries()

    assert len(entries) == 1
    assert entries[0]["candidate_voltage_mv"] == 875


def test_auto_uv_artifacts_write_to_main_profile_and_config_roots(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_root = tmp_path / "config-root"
    saved_uv_root = tmp_path / "saved-uv-root"
    monkeypatch.setattr(
        auto_uv_profiles,
        "default_user_config_dir",
        lambda: config_root,
    )
    monkeypatch.setattr(
        auto_uv_artifact_paths,
        "default_user_config_dir",
        lambda: config_root,
    )
    monkeypatch.setattr(
        auto_uv_artifact_paths,
        "default_saved_uv_dir",
        lambda: saved_uv_root,
    )
    probe = _probe(
        requested_mv=900,
        measured_mv=890.0,
        fps=170.0,
        power_w=240.0,
        temp_c=64.0,
    )
    base_probe = _probe(
        requested_mv=1000,
        measured_mv=1000.0,
        fps=150.0,
        power_w=300.0,
        temp_c=70.0,
        core_clock_mhz=2650.0,
    )
    final_fan_payload = {"fan": {"curve": [[45.0, 0.0], [70.0, 40.0]]}}

    final_path = auto_uv_artifacts._write_final_curve_snapshot(
        plan=_plan(),
        lock_clock_mhz=2445,
        voltage_mv=900,
        probe=probe,
        base_probe=base_probe,
        fan_curve_payload=final_fan_payload,
    )
    saved_path = auto_uv_artifacts._write_saved_uv_state(
        plan=_plan(),
        lock_clock_mhz=2445,
        voltage_mv=900,
        probe=probe,
        label="saved-best",
    )
    marker_path = auto_uv_artifacts._write_uv_probe_in_progress(
        phase="candidate",
        candidate_voltage_mv=900,
        lock_clock_mhz=2445,
    )
    unsafe_path, _entry = auto_uv_artifacts._record_unsafe_uv_voltage(
        candidate_voltage_mv=875,
        lock_clock_mhz=2430,
        reason="test",
    )
    fan_result = write_auto_uv_fan_payload(final_probe=probe, probes=[probe])

    assert final_path.parent == config_root / "auto-uv-profiles"
    assert final_path.name.startswith("auto-uv-profile-")
    final_payload = json.loads(final_path.read_text(encoding="utf-8"))
    assert final_payload["base_candidate_voltage_mv"] == 1000
    assert final_payload["base_lock_clock_mhz"] == 2700
    assert final_payload["base_avg_core_clock_mhz"] == 2650.0
    assert final_payload["base_avg_fps"] == 150.0
    assert final_payload["base_avg_power_w"] == 300.0
    assert final_payload["base_efficiency_fps_per_w"] == 0.5
    assert final_payload["fan_curve_payload"] == final_fan_payload
    assert fan_result is not None
    assert fan_result.path == config_root / "auto-uv-fan-curve.json"
    assert saved_path.parent == saved_uv_root
    assert marker_path == config_root / "uv-result" / "auto-uv-probe-in-progress.json"
    assert unsafe_path == config_root / "uv-result" / "auto-uv-unsafe-voltages.json"
    assert not (tmp_path / "auto-uv-final-curve.json").exists()


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
    assert payload["block_reason"] == "base-load-temperature-too-high"


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


def test_auto_uv_fan_payload_includes_measured_probe_points() -> None:
    baseline_probe = _probe(
        requested_mv=950,
        measured_mv=940.0,
        fps=160.0,
        power_w=260.0,
        temp_c=64.0,
    )
    candidate_probe = _probe(
        requested_mv=900,
        measured_mv=890.0,
        fps=170.0,
        power_w=240.0,
        temp_c=62.5,
    )
    baseline_probe.avg_fan_speed_pct = 42.0
    candidate_probe.avg_fan_speed_pct = 36.5

    payload = build_auto_uv_fan_payload(
        final_probe=candidate_probe,
        probes=[baseline_probe, candidate_probe],
    )

    assert payload is not None
    assert payload["telemetry"]["measured_fan_points"] == [
        {
            "temperature_c": 64.0,
            "fan_speed_pct": 42.0,
            "voltage_mv": 950,
            "clock_mhz": 2700,
        },
        {
            "temperature_c": 62.5,
            "fan_speed_pct": 36.5,
            "voltage_mv": 900,
            "clock_mhz": 2700,
        },
    ]
