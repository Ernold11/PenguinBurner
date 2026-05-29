from __future__ import annotations

import pytest

from auto_uv.scan_mode.efficiency_fps_per_w_policy import (
    compare_temperature_normalized_fps_per_w,
    decide_efficiency_stop,
    derive_efficiency_stop_streak_from_fps_variance,
)
from auto_uv_test_data import probe_summary


def test_efficiency_policy_temperature_normalizes_fps_per_w() -> None:
    delta = compare_temperature_normalized_fps_per_w(
        probe_summary(
            1000,
            clock_mhz=2200.0,
            fps=100.0,
            power_w=200.0,
            temperature_c=60.0,
        ),
        probe_summary(
            950,
            clock_mhz=2200.0,
            fps=100.0,
            power_w=190.0,
            temperature_c=65.0,
        ),
    )

    assert delta["improved"] is True
    assert delta["measured_voltage_close_to_requested"] is True


def test_efficiency_policy_requires_one_percent_fps_per_w_gain() -> None:
    delta = compare_temperature_normalized_fps_per_w(
        probe_summary(
            1000,
            clock_mhz=2200.0,
            fps=100.0,
            power_w=200.0,
        ),
        probe_summary(
            950,
            clock_mhz=2200.0,
            fps=100.75,
            power_w=200.0,
        ),
    )

    assert delta["delta_pct"] == pytest.approx(0.75)
    assert delta["improved"] is False


def test_efficiency_stop_reverts_to_previous_curve_after_confirmed_wall() -> None:
    decision = decide_efficiency_stop(
        efficiency_stop_candidate=True,
        voltage_drop_from_start_pct=12.0,
        min_voltage_drop_pct=10.0,
        no_gain_streak=3,
        required_extra_confirmations=1,
        pending_previous_curve=True,
        power_up_efficiency_down=True,
        efficiency_delta_pct=-0.2,
    )

    assert decision.should_stop
    assert not decision.use_current_curve
    assert decision.reason == "fps-per-watt wall reached"


def test_efficiency_stop_streak_uses_two_confirmations_for_low_fps_variance() -> None:
    decision = derive_efficiency_stop_streak_from_fps_variance(
        {"fps_variance_pct": 1.2},
        configured_streak=2,
        derive=True,
        high_variance_threshold_pct=2.0,
        low_variance_streak=2,
        high_variance_streak=4,
    )

    assert decision.value == 2
    assert decision.source == "low-fps-variance"


def test_efficiency_stop_streak_uses_four_confirmations_for_high_fps_variance() -> None:
    decision = derive_efficiency_stop_streak_from_fps_variance(
        {"fps_variance_pct": 2.1},
        configured_streak=2,
        derive=True,
        high_variance_threshold_pct=2.0,
        low_variance_streak=2,
        high_variance_streak=4,
    )

    assert decision.value == 4
    assert decision.source == "high-fps-variance"


def test_efficiency_stop_streak_keeps_user_override() -> None:
    decision = derive_efficiency_stop_streak_from_fps_variance(
        {"fps_variance_pct": 3.0},
        configured_streak=1,
        derive=False,
    )

    assert decision.value == 1
    assert decision.source == "user"
