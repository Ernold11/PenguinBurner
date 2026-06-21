from __future__ import annotations

"""Branch-coverage tests for the base-load pure-logic modules.

These exercise the edge cases (empty inputs, idle GPUs, percentile boundaries,
and the end-to-end plan builder) that the behavioral tests do not reach.
"""

import pytest

from auto_uv.curve.base_load_flatten_target import (
    CurveTiming,
    _avg_metric,
    _format_metric,
    _format_power_limit,
    _selected_gpu_idle_hint,
    choose_sustained_curve_clock,
    selected_nvidia_light_load_diagnostic,
    snap_clock_at_or_below,
)
from auto_uv.curve.base_load_probe_curve_plan import (
    BaseLoadCurvePlan,
    plan_base_load_curve_from_telemetry,
)
from auto_uv.curve.base_load_telemetry import (
    LoadedTelemetryRules,
    decision_samples,
    percentile,
    saturated_tail_samples,
)
from auto_uv.curve.base_load_voltage import (
    LoadedVoltageBand,
    derive_loaded_voltage_band,
)
from auto_uv_test_data import base_curve


# --------------------------------------------------------------------------
# base_load_flatten_target.py
# --------------------------------------------------------------------------


def test_choose_sustained_curve_clock_rejects_empty_base_curve() -> None:
    # Line 108: a base curve without any target clocks is unusable.
    with pytest.raises(ValueError, match="any target clocks"):
        choose_sustained_curve_clock([], 1800.0)


def test_snap_clock_at_or_below_rejects_non_positive() -> None:
    # Line 118: a zero/negative measured clock is nonsensical and must raise.
    with pytest.raises(ValueError, match="clock must be positive"):
        snap_clock_at_or_below(0.0)


def test_light_load_diagnostic_none_when_no_power_limit() -> None:
    # Line 132: without a positive power limit there is nothing to compare to.
    assert (
        selected_nvidia_light_load_diagnostic(
            [{"elapsed_s": 6.0, "power_w": 30.0, "gpu_util_pct": 99.0}],
            power_limit_w=None,
        )
        is None
    )
    assert (
        selected_nvidia_light_load_diagnostic(
            [{"elapsed_s": 6.0, "power_w": 30.0, "gpu_util_pct": 99.0}],
            power_limit_w=0,
        )
        is None
    )


def test_light_load_diagnostic_none_when_missing_metrics() -> None:
    # Lines 136-137: no power/util metrics at all -> no diagnostic.
    assert (
        selected_nvidia_light_load_diagnostic(
            [{"elapsed_s": 6.0}],
            power_limit_w=110,
        )
        is None
    )


def test_light_load_diagnostic_none_when_gpu_not_busy() -> None:
    # Line 139: utilization below the busy threshold means it is genuinely idle.
    assert (
        selected_nvidia_light_load_diagnostic(
            [{"elapsed_s": 6.0, "power_w": 10.0, "gpu_util_pct": 20.0}],
            power_limit_w=110,
        )
        is None
    )


def test_light_load_diagnostic_none_when_power_near_limit() -> None:
    # Line 145: busy GPU drawing >= 50% of its limit is healthy, not light-load.
    assert (
        selected_nvidia_light_load_diagnostic(
            [{"elapsed_s": 6.0, "power_w": 90.0, "gpu_util_pct": 99.0}],
            power_limit_w=110,
        )
        is None
    )


def test_idle_hint_empty_when_metric_missing() -> None:
    # Line 204: any missing metric leaves the idle hint silent.
    assert _selected_gpu_idle_hint([{"gpu_util_pct": 1.0}], power_limit_w=110) == ""


def test_idle_hint_empty_when_clock_is_high() -> None:
    # Line 208: a high clock means the GPU was not idle.
    samples = [{"gpu_util_pct": 1.0, "power_w": 5.0, "core_clock_mhz": 1800.0}]
    assert _selected_gpu_idle_hint(samples, power_limit_w=110) == ""


def test_idle_hint_empty_when_power_above_idle_floor() -> None:
    # Line 214: low util/clock but high power -> not the idle multi-GPU case.
    samples = [{"gpu_util_pct": 1.0, "power_w": 80.0, "core_clock_mhz": 300.0}]
    assert _selected_gpu_idle_hint(samples, power_limit_w=110) == ""


def test_idle_hint_present_for_truly_idle_selected_gpu() -> None:
    samples = [{"gpu_util_pct": 1.0, "power_w": 5.0, "core_clock_mhz": 300.0}]
    hint = _selected_gpu_idle_hint(samples, power_limit_w=110)
    assert "--gpu-index" in hint
    # No power limit means the power gate is skipped but hint still produced.
    assert "--gpu-index" in _selected_gpu_idle_hint(samples, power_limit_w=None)


def test_avg_metric_none_when_empty() -> None:
    # Line 230: no values -> no average.
    assert _avg_metric([], "power_w") is None
    assert _avg_metric([{"power_w": 10.0}, {"power_w": 20.0}], "power_w") == 15.0


def test_format_power_limit_handles_missing_and_present() -> None:
    # Line 236.
    assert _format_power_limit(None) == "n/a"
    assert _format_power_limit(0) == "n/a"
    assert _format_power_limit(110) == "110W"


def test_format_metric_handles_none() -> None:
    # Line 242.
    assert _format_metric(None, "W") == "n/a"
    assert _format_metric(12.34, "W") == "12.3W"


# --------------------------------------------------------------------------
# base_load_probe_curve_plan.py
# --------------------------------------------------------------------------


def test_plan_base_load_curve_from_telemetry_builds_full_plan() -> None:
    # Lines 30-44: end-to-end plan builder over real loaded telemetry.
    curve = base_curve(850, 1000, 25, 1770, 15)
    telemetry = [
        {"elapsed_s": 6.0, "power_w": 190.0, "core_clock_mhz": 1848.0, "voltage_mv": 900},
        {"elapsed_s": 7.0, "power_w": 196.0, "core_clock_mhz": 1842.0, "voltage_mv": 905},
        {"elapsed_s": 8.0, "power_w": 200.0, "core_clock_mhz": 1837.0, "voltage_mv": 910},
    ]

    plan = plan_base_load_curve_from_telemetry(
        curve,
        telemetry,
        start_voltage_mv=900,
        power_limit_w=200,
        fallback_clock_mhz=1905.0,
        tail_rise_bins=0,
        curve=CurveTiming(),
    )

    assert isinstance(plan, BaseLoadCurvePlan)
    assert plan.target.target_clock_mhz == 1830
    assert plan.flatten_target["lock_clock_mhz"] == plan.target.target_clock_mhz
    by_voltage = {point["voltage_mv"]: point for point in plan.flattened_plan}
    assert by_voltage[900]["target_mhz"] == plan.target.target_clock_mhz


# --------------------------------------------------------------------------
# base_load_telemetry.py
# --------------------------------------------------------------------------


def test_decision_samples_returns_all_when_warmup_disabled() -> None:
    # Line 29: warmup <= 0 keeps every sample unfiltered.
    rules = LoadedTelemetryRules(loaded_sample_warmup_s=0.0)
    samples = [{"elapsed_s": 0.0}, {"elapsed_s": 1.0}]
    assert decision_samples(samples, rules=rules) == samples


def test_saturated_tail_breaks_after_trailing_run() -> None:
    # Lines 69-70: once a contiguous saturated tail is found, scanning backwards
    # stops at the first non-saturated sample below it.
    rules = LoadedTelemetryRules(loaded_sample_warmup_s=0.0, saturated_tail_min_samples=2)
    samples = [
        {"elapsed_s": 1.0, "power_w": 200.0, "core_clock_mhz": 2000.0},  # saturated, isolated
        {"elapsed_s": 2.0, "power_w": 50.0, "core_clock_mhz": 1000.0},   # gap -> break point
        {"elapsed_s": 3.0, "power_w": 198.0, "core_clock_mhz": 1990.0},  # tail
        {"elapsed_s": 4.0, "power_w": 200.0, "core_clock_mhz": 2000.0},  # tail
    ]
    tail = saturated_tail_samples(samples, rules=rules)
    assert [s["elapsed_s"] for s in tail] == [3.0, 4.0]


def test_saturated_tail_falls_back_to_scattered_then_all() -> None:
    # Lines 76-77: the trailing run is too short, so use scattered saturated
    # samples; if those are also too few, fall back to all samples.
    rules = LoadedTelemetryRules(loaded_sample_warmup_s=0.0, saturated_tail_min_samples=2)
    samples = [
        {"elapsed_s": 1.0, "power_w": 200.0, "core_clock_mhz": 2000.0},  # saturated
        {"elapsed_s": 2.0, "power_w": 198.0, "core_clock_mhz": 1990.0},  # saturated
        {"elapsed_s": 3.0, "power_w": 50.0, "core_clock_mhz": 1000.0},   # ends with idle
    ]
    tail = saturated_tail_samples(samples, rules=rules)
    # Trailing run is the single idle-broken sample at index 1 only, < min_samples,
    # so the scattered-saturated branch returns the two saturated samples.
    assert [s["elapsed_s"] for s in tail] == [1.0, 2.0]

    # Now make scattered saturated also insufficient -> return all samples.
    rules_high = LoadedTelemetryRules(
        loaded_sample_warmup_s=0.0, saturated_tail_min_samples=5
    )
    assert saturated_tail_samples(samples, rules=rules_high) == samples


def test_percentile_requires_values_and_handles_single() -> None:
    # Line 167: empty raises.
    with pytest.raises(ValueError, match="at least one value"):
        percentile([], 0.5)
    # Line 170: a single value is returned directly.
    assert percentile([42.0], 0.5) == 42.0


# --------------------------------------------------------------------------
# base_load_voltage.py
# --------------------------------------------------------------------------


def test_loaded_voltage_band_empty_when_no_power_floor() -> None:
    band = derive_loaded_voltage_band(
        [],
        power_limit_w=None,
        use_power_limit_floor=False,
    )
    assert band == LoadedVoltageBand(None)


def test_loaded_voltage_band_empty_when_no_voltages_pass_floor() -> None:
    # Line 59: samples exist (so the floor is derived) but none carry voltage,
    # so the voltage list is empty.
    band = derive_loaded_voltage_band(
        [{"elapsed_s": 6.0, "power_w": 200.0}],
        power_limit_w=200,
        use_power_limit_floor=True,
    )
    assert band == LoadedVoltageBand(None)


def test_loaded_voltage_band_summarizes_passing_samples() -> None:
    band = derive_loaded_voltage_band(
        [
            {"elapsed_s": 6.0, "power_w": 200.0, "voltage_mv": 900.0},
            {"elapsed_s": 7.0, "power_w": 200.0, "voltage_mv": 910.0},
            {"elapsed_s": 8.0, "power_w": 200.0, "voltage_mv": 920.0},
        ],
        power_limit_w=200,
        use_power_limit_floor=True,
    )
    assert band.average_mv == 910
