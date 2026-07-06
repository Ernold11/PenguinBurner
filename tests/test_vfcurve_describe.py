"""Unit tests for the Afterburner VF-curve description (presentation) helpers."""
from __future__ import annotations

from integrations.afterburner.vfcurve_describe import (
    describe_afterburner_dynamic_lock,
    describe_afterburner_flatten_validation,
)


def test_describe_dynamic_lock_disabled():
    assert describe_afterburner_dynamic_lock(None) == "disabled"
    assert describe_afterburner_dynamic_lock({}) == "disabled"


def test_describe_dynamic_lock_defaults_unknown_source():
    assert (
        describe_afterburner_dynamic_lock({"lock_clock_mhz": 1800})
        == "source=unknown, lock=1800MHz"
    )


def test_describe_dynamic_lock_full():
    text = describe_afterburner_dynamic_lock(
        {
            "source": "tail",
            "lock_clock_mhz": 1800,
            "lock_voltage_mv": 900,
            "end_voltage_mv": 1000,
            "tail_point_count": 5,
        }
    )
    assert text == "source=tail, lock=1800MHz@900mV, tail=900-1000mV, points=5"


def test_describe_flatten_validation_unknown_and_invalid():
    assert describe_afterburner_flatten_validation(None) == "unknown"
    assert (
        describe_afterburner_flatten_validation({"valid": False, "reason": "no baseline"})
        == "no baseline"
    )
    assert describe_afterburner_flatten_validation({"valid": False}) == "invalid"


def test_describe_flatten_validation_valid_minimal():
    text = describe_afterburner_flatten_validation(
        {
            "valid": True,
            "baseline_section": "v3",
            "selected_clock_mhz": 1800,
            "selected_voltage_mv": 900,
            "baseline_required_voltage_mv": 950,
            "undervolt_margin_mv": 50.0,
        }
    )
    assert text == (
        "baseline=v3, target=1800MHz@900mV, default-same-clock=950mV, uv-margin=+50mV"
    )


def test_describe_flatten_validation_valid_with_same_voltage_delta():
    text = describe_afterburner_flatten_validation(
        {
            "valid": True,
            "baseline_section": "v3",
            "selected_clock_mhz": 1800,
            "selected_voltage_mv": 900,
            "baseline_required_voltage_mv": 950,
            "undervolt_margin_mv": 50.0,
            "baseline_same_voltage_clock_mhz": 1850,
            "same_voltage_delta_mhz": 50.0,
        }
    )
    assert text.endswith(
        "default@same-voltage=1850MHz, same-voltage-delta=+50MHz"
    )
