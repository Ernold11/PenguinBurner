"""Unit tests for the Afterburner VF-curve description (presentation) helpers."""
from __future__ import annotations

from afterburner.vfcurve_describe import (
    _format_curve_float,
    describe_afterburner_dynamic_lock,
    describe_afterburner_flatten_validation,
    describe_afterburner_profile_settings,
    describe_afterburner_vfcurve_analysis,
)


def test_format_curve_float_renders_near_integers_as_integers():
    assert _format_curve_float(5.0) == "5"
    assert _format_curve_float(5.0000001) == "5"
    assert _format_curve_float(1234.0) == "1234"


def test_format_curve_float_trims_trailing_zeros():
    assert _format_curve_float(5.25) == "5.25"
    assert _format_curve_float(5.5) == "5.5"


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


def test_describe_vfcurve_analysis_all_zero_third_field():
    text = describe_afterburner_vfcurve_analysis(
        {
            "point_count": 80,
            "min_frequency_mhz": 300.0,
            "max_frequency_mhz": 1800.0,
            "nonzero_third_value_count": 0,
        }
    )
    assert text == "points=80, freq-range=300-1800MHz, third-field=all-zero"


def test_describe_vfcurve_analysis_nonzero_preview():
    text = describe_afterburner_vfcurve_analysis(
        {
            "point_count": 80,
            "min_frequency_mhz": 300.0,
            "max_frequency_mhz": 1800.0,
            "nonzero_third_value_count": 3,
            "unique_nonzero_third_values": [1.0, 2.5, 3.0],
            "adjusted_anchor": None,
        }
    )
    assert text == (
        "points=80, freq-range=300-1800MHz, third-field=nonzero(1, 2.5, 3; 3 point(s))"
    )


def test_describe_vfcurve_analysis_truncates_long_preview():
    text = describe_afterburner_vfcurve_analysis(
        {
            "point_count": 80,
            "min_frequency_mhz": 300.0,
            "max_frequency_mhz": 1800.0,
            "nonzero_third_value_count": 10,
            "unique_nonzero_third_values": [1.0, 2.0, 3.0, 4.0, 5.0],
            "adjusted_anchor": None,
        }
    )
    assert "third-field=nonzero(1, 2, 3, 4, ...; 10 point(s))" in text


def test_describe_vfcurve_analysis_with_adjusted_anchor():
    text = describe_afterburner_vfcurve_analysis(
        {
            "point_count": 80,
            "min_frequency_mhz": 300.0,
            "max_frequency_mhz": 1800.0,
            "nonzero_third_value_count": 3,
            "unique_nonzero_third_values": [1.0],
            "adjusted_anchor": {
                "voltage_mv": 900.0,
                "frequency_mhz": 1800.0,
                "clamp_start_voltage_mv": 850.0,
                "clamped_point_count": 4,
            },
        }
    )
    assert text.endswith(
        "decoded=adjusted-anchor(900mV/1800MHz, clamp-from=850mV, clamped=4)"
    )


def test_describe_profile_settings_empty_is_none():
    assert describe_afterburner_profile_settings({}) == "none"


def test_describe_profile_settings_blank_thermal_limit_is_skipped():
    assert describe_afterburner_profile_settings({"thermal_limit_raw": "  "}) == "none"


def test_describe_profile_settings_fan_partial_fields():
    assert describe_afterburner_profile_settings({"fan_mode": 1}) == "fan1=mode1"
    assert describe_afterburner_profile_settings({"fan_speed_pct": 70}) == "fan1=@70%"


def test_describe_profile_settings_full():
    text = describe_afterburner_profile_settings(
        {
            "power_limit_pct": 90,
            "core_clk_boost_khz": 150000,
            "mem_clk_boost_khz": 1000000,
            "thermal_limit_raw": "83",
            "fan_mode": 1,
            "fan_speed_pct": 70,
            "fan_mode2": 2,
            "fan_speed2_pct": 65,
        }
    )
    assert text == (
        "power-limit=90%, core-boost=150000kHz, mem-boost=1000000kHz, "
        "thermal-limit=83, fan1=mode1@70%, fan2=mode2@65%"
    )
