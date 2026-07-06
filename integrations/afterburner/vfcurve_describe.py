#!/usr/bin/env python3
"""Human-readable descriptions of Afterburner VF-curve data.

Pure presentation helpers: each formats a plain dict produced elsewhere in
:mod:`afterburner.vfcurve` and holds no parsing/analysis logic, so they live
apart from it and import nothing from it.
"""
from __future__ import annotations


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
