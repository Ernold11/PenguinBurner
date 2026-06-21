#!/usr/bin/env python3
"""Human-readable descriptions of Afterburner VF-curve data.

Pure presentation helpers: each formats a plain dict produced elsewhere in
:mod:`afterburner.vfcurve` and holds no parsing/analysis logic, so they live
apart from it and import nothing from it.
"""
from __future__ import annotations

# Display rounding tolerance: a curve float within this of an integer renders as
# an integer. Equivalent in value to vfcurve.THIRD_VALUE_EPSILON but a separate
# presentation concern.
_CURVE_DISPLAY_EPSILON = 1e-6


def _format_curve_float(value: float) -> str:
    if abs(value - round(value)) < _CURVE_DISPLAY_EPSILON:
        return str(int(round(value)))
    return f"{value:.3f}".rstrip("0").rstrip(".")


def describe_afterburner_dynamic_lock(dynamic_lock):
    if not dynamic_lock:
        return "disabled"

    parts = [
        f"source={dynamic_lock.get('source', 'unknown')}",
        f"lock={int(dynamic_lock['lock_clock_mhz'])}MHz",
    ]
    lock_voltage_mv = dynamic_lock.get("lock_voltage_mv")
    if lock_voltage_mv is not None:
        parts[-1] += f"@{int(lock_voltage_mv)}mV"

    end_voltage_mv = dynamic_lock.get("end_voltage_mv")
    if end_voltage_mv is not None and lock_voltage_mv is not None:
        parts.append(f"tail={int(lock_voltage_mv)}-{int(end_voltage_mv)}mV")

    tail_point_count = dynamic_lock.get("tail_point_count")
    if tail_point_count:
        parts.append(f"points={int(tail_point_count)}")

    return ", ".join(parts)


def describe_afterburner_flatten_validation(validation):
    if not validation:
        return "unknown"

    if not validation.get("valid"):
        return str(validation.get("reason", "invalid"))

    parts = [
        f"baseline={validation['baseline_section']}",
        (
            f"target={int(validation['selected_clock_mhz'])}MHz"
            f"@{int(validation['selected_voltage_mv'])}mV"
        ),
        f"default-same-clock={int(validation['baseline_required_voltage_mv'])}mV",
        f"uv-margin=+{int(round(float(validation['undervolt_margin_mv'])))}mV",
    ]
    baseline_same_voltage_clock_mhz = validation.get("baseline_same_voltage_clock_mhz")
    same_voltage_delta_mhz = validation.get("same_voltage_delta_mhz")
    if (
        baseline_same_voltage_clock_mhz is not None
        and same_voltage_delta_mhz is not None
    ):
        parts.append(f"default@same-voltage={int(baseline_same_voltage_clock_mhz)}MHz")
        parts.append(
            f"same-voltage-delta={int(round(float(same_voltage_delta_mhz))):+d}MHz"
        )
    return ", ".join(parts)


def describe_afterburner_vfcurve_analysis(analysis):
    parts = [
        f"points={analysis['point_count']}",
        (
            "freq-range="
            f"{_format_curve_float(analysis['min_frequency_mhz'])}-"
            f"{_format_curve_float(analysis['max_frequency_mhz'])}MHz"
        ),
    ]

    if analysis["nonzero_third_value_count"] == 0:
        parts.append("third-field=all-zero")
    else:
        preview = ", ".join(
            _format_curve_float(value)
            for value in analysis["unique_nonzero_third_values"][:4]
        )
        if len(analysis["unique_nonzero_third_values"]) > 4:
            preview += ", ..."
        parts.append(
            "third-field=nonzero("
            f"{preview}; "
            f"{analysis['nonzero_third_value_count']} point(s))"
        )
        if analysis["adjusted_anchor"] is not None:
            anchor = analysis["adjusted_anchor"]
            parts.append(
                "decoded=adjusted-anchor("
                f"{_format_curve_float(anchor['voltage_mv'])}mV/"
                f"{_format_curve_float(anchor['frequency_mhz'])}MHz, "
                f"clamp-from={_format_curve_float(anchor['clamp_start_voltage_mv'])}mV, "
                f"clamped={anchor['clamped_point_count']})"
            )

    return ", ".join(parts)


def describe_afterburner_profile_settings(settings):
    parts = []

    power_limit_pct = settings.get("power_limit_pct")
    if power_limit_pct is not None:
        parts.append(f"power-limit={int(power_limit_pct)}%")

    core_clk_boost_khz = settings.get("core_clk_boost_khz")
    if core_clk_boost_khz is not None:
        parts.append(f"core-boost={int(core_clk_boost_khz)}kHz")

    mem_clk_boost_khz = settings.get("mem_clk_boost_khz")
    if mem_clk_boost_khz is not None:
        parts.append(f"mem-boost={int(mem_clk_boost_khz)}kHz")

    thermal_limit_raw = str(settings.get("thermal_limit_raw", "")).strip()
    if thermal_limit_raw:
        parts.append(f"thermal-limit={thermal_limit_raw}")

    fan_mode = settings.get("fan_mode")
    fan_speed_pct = settings.get("fan_speed_pct")
    if fan_mode is not None or fan_speed_pct is not None:
        fan_text = "fan1="
        if fan_mode is not None:
            fan_text += f"mode{int(fan_mode)}"
        if fan_speed_pct is not None:
            fan_text += f"@{int(fan_speed_pct)}%"
        parts.append(fan_text)

    fan_mode2 = settings.get("fan_mode2")
    fan_speed2_pct = settings.get("fan_speed2_pct")
    if fan_mode2 is not None or fan_speed2_pct is not None:
        fan_text = "fan2="
        if fan_mode2 is not None:
            fan_text += f"mode{int(fan_mode2)}"
        if fan_speed2_pct is not None:
            fan_text += f"@{int(fan_speed2_pct)}%"
        parts.append(fan_text)

    return ", ".join(parts) if parts else "none"
