"""Fan curve validation and runtime speed decisions.

These rules are pure math: no NVML calls, no config files, and no GPU side effects.
"""

from __future__ import annotations

from common.penguin_burner_errors import NvmlError


def clamp(value, lower, upper):
    return max(lower, min(value, upper))


def validate_curve(curve) -> None:
    if len(curve) < 2:
        raise NvmlError("curve must contain at least two points")

    last_temp = None
    last_speed = None
    for temp_c, speed_pct in curve:
        if last_temp is not None and temp_c <= last_temp:
            raise NvmlError("curve temperatures must be strictly increasing")
        if not 0 <= speed_pct <= 100:
            raise NvmlError("curve fan speeds must be in the range 0..100")
        if last_speed is not None and speed_pct < last_speed:
            raise NvmlError("curve fan speeds must not decrease as temperature rises")
        last_temp = temp_c
        last_speed = speed_pct


def format_curve_points(curve):
    return ", ".join(f"{temp_c:.0f}C->{speed_pct:.0f}%" for temp_c, speed_pct in curve)


def build_effective_manual_curve(
    curve,
    manual_enable_temp_c,
    effective_min_fan_speed_pct,
    effective_max_fan_speed_pct,
    mode,
):
    start_speed_pct = clamp(
        speed_for_temp(manual_enable_temp_c, curve, mode=mode),
        effective_min_fan_speed_pct,
        effective_max_fan_speed_pct,
    )
    effective_curve = [(float(manual_enable_temp_c), float(start_speed_pct))]

    for temp_c, speed_pct in curve:
        if temp_c <= manual_enable_temp_c:
            continue
        clamped_speed_pct = clamp(
            float(speed_pct),
            effective_min_fan_speed_pct,
            effective_max_fan_speed_pct,
        )
        last_temp_c, last_speed_pct = effective_curve[-1]
        if abs(clamped_speed_pct - last_speed_pct) < 0.001:
            continue
        effective_curve.append((float(temp_c), float(clamped_speed_pct)))

    return effective_curve


def speed_for_temp(temp_c, curve, mode):
    if temp_c <= curve[0][0]:
        return curve[0][1]

    for index in range(1, len(curve)):
        left_temp, left_speed = curve[index - 1]
        right_temp, right_speed = curve[index]

        if temp_c <= right_temp:
            if mode == "step":
                return left_speed

            span = right_temp - left_temp
            t = (temp_c - left_temp) / span
            return left_speed + (right_speed - left_speed) * t

    return curve[-1][1]


def apply_hysteresis(
    current_temp_c, raw_target_speed, last_temp_c, last_speed, hysteresis_c
):
    if last_temp_c is None or last_speed is None or hysteresis_c <= 0.0:
        return raw_target_speed

    if raw_target_speed >= last_speed:
        return raw_target_speed

    if current_temp_c > last_temp_c:
        return raw_target_speed

    if (last_temp_c - current_temp_c) < hysteresis_c:
        return float(last_speed)

    return raw_target_speed


def limit_speed_change(
    target_speed, last_speed, elapsed_s, max_step_up_pct_per_s, max_step_down_pct_per_s
):
    if last_speed is None or elapsed_s <= 0.0:
        return target_speed

    limited_speed = float(target_speed)

    if limited_speed > last_speed and max_step_up_pct_per_s > 0.0:
        max_up = max_step_up_pct_per_s * elapsed_s
        limited_speed = min(limited_speed, last_speed + max_up)

    if limited_speed < last_speed and max_step_down_pct_per_s > 0.0:
        max_down = max_step_down_pct_per_s * elapsed_s
        limited_speed = max(limited_speed, last_speed - max_down)

    return limited_speed
