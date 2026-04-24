#!/usr/bin/env python3

from __future__ import annotations

import struct
from pathlib import Path

from penguin_burner_paths import (
    afterburner_global_profile,
    afterburner_profiles_dir,
    default_afterburner_global_profile,
)

DEFAULT_GLOBAL_PROFILE = default_afterburner_global_profile()
SW_AUTO_FAN_CONTROL_FLAG_FORCE_UPDATE = 0x1
SW_AUTO_FAN_CONTROL_FLAG_OVERRIDE_ZERO_WITH_HARDWARE_CURVE = 0x4


def _find_value(lines, key):
    for line in lines:
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    raise KeyError(f"{key} not found")


def parse_sw_auto_fan_curve(hex_blob: str):
    data = bytes.fromhex(hex_blob.strip())
    magic_u32 = struct.unpack_from("<I", data, 0)[0]
    point_count = struct.unpack_from("<I", data, 4)[0]
    flags_u32 = struct.unpack_from("<I", data, 8)[0]

    points = []
    for index in range(point_count):
        temp_c, speed_pct = struct.unpack_from("<ff", data, 12 + index * 8)
        points.append(
            {
                "index": index,
                "temperature_c": float(temp_c),
                "speed_pct": float(speed_pct),
            }
        )

    return {
        "magic_u32": magic_u32,
        "point_count": point_count,
        "flags_u32": flags_u32,
        "points": points,
    }


def _candidate_fan_profile_paths(afterburner_root=None):
    if afterburner_root is not None:
        root = Path(afterburner_root).expanduser()
        return [
            afterburner_profiles_dir(root) / "MSIAfterburner.cfg",
            afterburner_global_profile(root),
        ]
    return [DEFAULT_GLOBAL_PROFILE]


def resolve_afterburner_fan_profile(
    profile_path=DEFAULT_GLOBAL_PROFILE, afterburner_root=None
):
    if afterburner_root is not None:
        candidates = _candidate_fan_profile_paths(afterburner_root)
    else:
        resolved = Path(profile_path).expanduser()
        if resolved.is_dir():
            candidates = _candidate_fan_profile_paths(resolved)
        else:
            candidates = [resolved]

    first_existing = None
    for candidate in candidates:
        if not candidate.is_file():
            continue
        if first_existing is None:
            first_existing = candidate
        lines = candidate.read_text(errors="ignore").splitlines()
        if any(line.startswith("SwAutoFanControl=") for line in lines):
            return candidate

    if first_existing is not None:
        return first_existing

    return candidates[0]


def load_afterburner_fan_settings(profile_path=DEFAULT_GLOBAL_PROFILE):
    profile_path = resolve_afterburner_fan_profile(profile_path=profile_path)
    lines = profile_path.read_text(errors="ignore").splitlines()

    sw_auto_enabled = int(_find_value(lines, "SwAutoFanControl"))
    flags = int(_find_value(lines, "SwAutoFanControlFlags").rstrip("h"), 16)
    period_ms = int(_find_value(lines, "SwAutoFanControlPeriod"))
    curve = parse_sw_auto_fan_curve(_find_value(lines, "SwAutoFanControlCurve"))
    curve2 = parse_sw_auto_fan_curve(_find_value(lines, "SwAutoFanControlCurve2"))

    return {
        "profile_path": profile_path,
        "sw_auto_enabled": sw_auto_enabled,
        "flags_u32": flags,
        "flags": decode_sw_auto_fan_flags(flags),
        "period_ms": period_ms,
        "curve": curve,
        "curve2": curve2,
    }


def validate_afterburner_fan_settings(
    settings,
    *,
    device_min_fan_speed_pct=None,
    device_max_fan_speed_pct=None,
):
    problems = []
    if not int(settings.get("sw_auto_enabled", 0)):
        problems.append("Afterburner software auto fan control is disabled")

    primary_points = list(settings.get("curve", {}).get("points", []))
    reference_points = list(settings.get("curve2", {}).get("points", []))
    if len(primary_points) < 2:
        problems.append("primary fan curve must contain at least two points")
    if len(reference_points) < 2:
        problems.append("reference/default fan curve must contain at least two points")

    last_temp_c = None
    last_speed_pct = None
    for index, point in enumerate(primary_points):
        temp_c = float(point["temperature_c"])
        speed_pct = float(point["speed_pct"])
        if temp_c < 0.0 or temp_c > 120.0:
            problems.append(
                f"primary fan point {index} has invalid temperature {temp_c:.1f}C"
            )
        if speed_pct < 0.0 or speed_pct > 100.0:
            problems.append(
                f"primary fan point {index} has invalid speed {speed_pct:.1f}%"
            )
        if last_temp_c is not None and temp_c < last_temp_c:
            problems.append("primary fan curve temperatures must be nondecreasing")
        if last_speed_pct is not None and speed_pct < last_speed_pct:
            problems.append("primary fan curve speeds must be nondecreasing")
        last_temp_c = temp_c
        last_speed_pct = speed_pct

    if primary_points and device_min_fan_speed_pct is not None:
        manual_points = [
            float(point["speed_pct"])
            for point in primary_points
            if float(point["speed_pct"]) >= float(device_min_fan_speed_pct)
        ]
        if not manual_points:
            problems.append(
                f"primary fan curve never reaches the device manual minimum "
                f"{float(device_min_fan_speed_pct):.0f}%"
            )

    if primary_points and device_max_fan_speed_pct is not None:
        highest_speed_pct = max(float(point["speed_pct"]) for point in primary_points)
        if highest_speed_pct > float(device_max_fan_speed_pct):
            problems.append(
                f"primary fan curve exceeds the device fan maximum "
                f"{float(device_max_fan_speed_pct):.0f}%"
            )

    if len(primary_points) >= 2 and len(reference_points) >= 2:
        overlap_start_c = max(
            float(primary_points[0]["temperature_c"]),
            float(reference_points[0]["temperature_c"]),
        )
        overlap_end_c = min(
            float(primary_points[-1]["temperature_c"]),
            float(reference_points[-1]["temperature_c"]),
        )
        quieter_than_reference = False
        if overlap_start_c <= overlap_end_c:
            sample_temps_c = sorted(
                {
                    float(point["temperature_c"])
                    for point in primary_points + reference_points
                    if overlap_start_c <= float(point["temperature_c"]) <= overlap_end_c
                }
            )
            for sample_temp_c in sample_temps_c:
                primary_speed_pct = speed_for_temperature(primary_points, sample_temp_c)
                reference_speed_pct = speed_for_temperature(
                    reference_points, sample_temp_c
                )
                if (
                    primary_speed_pct is not None
                    and reference_speed_pct is not None
                    and primary_speed_pct + 0.5 < reference_speed_pct
                ):
                    quieter_than_reference = True
                    break
        if not quieter_than_reference:
            problems.append(
                "primary fan curve is not quieter than the saved default/reference fan curve"
            )

    return {
        "valid": not problems,
        "problems": problems,
    }


def decode_sw_auto_fan_flags(flags_u32):
    return {
        "force_update_each_period": bool(
            flags_u32 & SW_AUTO_FAN_CONTROL_FLAG_FORCE_UPDATE
        ),
        "override_zero_with_hardware_curve": bool(
            flags_u32 & SW_AUTO_FAN_CONTROL_FLAG_OVERRIDE_ZERO_WITH_HARDWARE_CURVE
        ),
        "unknown_bits_u32": flags_u32
        & ~(
            SW_AUTO_FAN_CONTROL_FLAG_FORCE_UPDATE
            | SW_AUTO_FAN_CONTROL_FLAG_OVERRIDE_ZERO_WITH_HARDWARE_CURVE
        ),
    }


def temperature_for_speed(points, target_speed_pct):
    if not points:
        return None

    if target_speed_pct <= points[0]["speed_pct"]:
        return float(points[0]["temperature_c"])

    for left, right in zip(points, points[1:]):
        left_speed = float(left["speed_pct"])
        right_speed = float(right["speed_pct"])
        if target_speed_pct <= right_speed:
            if right_speed == left_speed:
                return float(right["temperature_c"])
            t = (target_speed_pct - left_speed) / (right_speed - left_speed)
            return float(
                left["temperature_c"]
                + (right["temperature_c"] - left["temperature_c"]) * t
            )

    return float(points[-1]["temperature_c"])


def speed_for_temperature(points, target_temp_c):
    if not points:
        return None

    if target_temp_c <= float(points[0]["temperature_c"]):
        return float(points[0]["speed_pct"])

    for left, right in zip(points, points[1:]):
        left_temp = float(left["temperature_c"])
        right_temp = float(right["temperature_c"])
        if target_temp_c <= right_temp:
            if right_temp == left_temp:
                return float(right["speed_pct"])
            t = (target_temp_c - left_temp) / (right_temp - left_temp)
            return float(
                left["speed_pct"] + (right["speed_pct"] - left["speed_pct"]) * t
            )

    return float(points[-1]["speed_pct"])


def highest_point_temperature_at_or_below_speed(points, max_speed_pct):
    matching_temps = [
        float(point["temperature_c"])
        for point in points
        if float(point["speed_pct"]) <= max_speed_pct
    ]
    return max(matching_temps) if matching_temps else None


def highest_zero_speed_temperature(points):
    zero_points = [
        float(point["temperature_c"])
        for point in points
        if float(point["speed_pct"]) <= 0.0
    ]
    return max(zero_points) if zero_points else None


def format_curve_points(points):
    return ", ".join(
        f"{point['temperature_c']:.0f}C->{point['speed_pct']:.0f}%" for point in points
    )
