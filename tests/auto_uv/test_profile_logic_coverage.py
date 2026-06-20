"""Branch-coverage tests for crucial Auto-UV profile/curve logic helpers.

These cover the small error/edge branches in the tail-shape, curve-flattening,
scan-settings, UV-limit, and tuning helpers that the tier logic depends on.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from auto_uv.auto_uv_types import AutoUvError
from auto_uv.auto_uv_user_options import AUTO_UV_DEFAULTS, AutoUvMetricTuning
from auto_uv.curve.rising_tail import normalize_tail_rise_bins
from auto_uv.curve.vf_curve_flattening import FlatteningRules, snap_target_clock
from auto_uv.scan_mode.uv_limits import (
    uv_limit_profile_target_for_gpu,
    voltage_drop_pct,
)
from auto_uv.scan_runtime_settings import (
    derive_efficiency_stop_streak,
    efficiency_stop_streak,
    read_scan_runtime_settings,
)


# --- rising_tail.normalize_tail_rise_bins ---
def test_normalize_tail_rise_bins_clamps_and_swallows_garbage() -> None:
    assert normalize_tail_rise_bins(4) == 4
    assert normalize_tail_rise_bins(-3) == 0
    assert normalize_tail_rise_bins(None) == 0
    assert normalize_tail_rise_bins("nope") == 0  # ValueError branch
    assert normalize_tail_rise_bins([1, 2]) == 0  # TypeError branch


# --- vf_curve_flattening.snap_target_clock ---
def test_snap_target_clock_rounds_to_step() -> None:
    rules = FlatteningRules()
    assert snap_target_clock(2603, rules=rules) == 2610
    assert snap_target_clock(2598, rules=rules) == 2595


def test_snap_target_clock_rejects_non_positive() -> None:
    rules = FlatteningRules()
    with pytest.raises(ValueError):
        snap_target_clock(0, rules=rules)
    with pytest.raises(ValueError):
        snap_target_clock(-15, rules=rules)


# --- scan_runtime_settings ---
def test_read_scan_runtime_settings_requires_positive_duration() -> None:
    cfg = SimpleNamespace(duration_s=0)
    with pytest.raises(AutoUvError):
        read_scan_runtime_settings({}, cfg)


def test_derive_efficiency_stop_streak_branches() -> None:
    # explicit flag -> not derived (False)
    assert derive_efficiency_stop_streak(
        {"auto_uv_efficiency_stop_streak_explicit": True}
    ) is False
    # absent value -> derived default (True)
    assert derive_efficiency_stop_streak({}) is True
    # value equal to default -> True
    assert derive_efficiency_stop_streak(
        {"auto_uv_efficiency_stop_streak": AUTO_UV_DEFAULTS.efficiency_stop_streak}
    ) is True
    # value differs from default -> False
    assert derive_efficiency_stop_streak(
        {"auto_uv_efficiency_stop_streak": AUTO_UV_DEFAULTS.efficiency_stop_streak + 1}
    ) is False
    # garbage value -> True (except branch)
    assert derive_efficiency_stop_streak(
        {"auto_uv_efficiency_stop_streak": "x"}
    ) is True


def test_efficiency_stop_streak_clamps_value() -> None:
    assert efficiency_stop_streak({"auto_uv_efficiency_stop_streak": 5}) == 5
    assert efficiency_stop_streak({"auto_uv_efficiency_stop_streak": -2}) == 0


# --- uv_limits ---
def test_uv_limit_profile_target_unknown_profile_returns_none() -> None:
    # GPU matches a family, but the requested profile is not defined for it
    assert uv_limit_profile_target_for_gpu("RTX 4070 Ti Super", "ultra") is None


def test_uv_limit_profile_target_unknown_gpu_returns_none() -> None:
    assert uv_limit_profile_target_for_gpu("Totally Unknown GPU", "efficiency") is None


def test_uv_limit_profile_target_known() -> None:
    target = uv_limit_profile_target_for_gpu("RTX 4070 Ti Super", "efficiency")
    assert target is not None
    assert target.voltage_mv == 925
    assert target.clock_mhz == 2550


def test_voltage_drop_pct_zero_start_returns_zero() -> None:
    assert voltage_drop_pct(start_voltage_mv=0, floor_voltage_mv=800) == 0.0


def test_voltage_drop_pct_normal() -> None:
    assert voltage_drop_pct(
        start_voltage_mv=1000, floor_voltage_mv=900
    ) == pytest.approx(10.0)


# --- auto_uv_user_options.AutoUvMetricTuning.max_core_clock_drop_pct ---
def test_max_core_clock_drop_pct_property() -> None:
    assert AutoUvMetricTuning(
        min_performance_core_clock_pct=85.0
    ).max_core_clock_drop_pct == pytest.approx(15.0)
    # clamps at zero when the min pct exceeds 100
    assert AutoUvMetricTuning(
        min_performance_core_clock_pct=120.0
    ).max_core_clock_drop_pct == 0.0
