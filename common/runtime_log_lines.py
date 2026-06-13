"""Pure formatters for the runtime daemon's journal lines.

Every record is a single ``| ``-delimited line with a short tag (see
``common.log_format``). These functions take primitive values and return the
final string, so the daemon loop only has to gather data and print — and the
formatting is fully unit-testable without a GPU.
"""

from __future__ import annotations

from common.log_format import format_log_line


def format_clock_voltage(core_clock_mhz=None, voltage_mv=None) -> str:
    """``2642MHz @ 860mV`` (or just one side, or empty if neither is known)."""
    clock = None if core_clock_mhz is None else f"{int(round(float(core_clock_mhz)))}MHz"
    volt = None if voltage_mv is None else f"{int(round(float(voltage_mv)))}mV"
    if clock and volt:
        return f"{clock} @ {volt}"
    return clock or volt or ""


def format_fan_section(fan_pct=None, fan_mode="auto") -> str:
    """``fan 35% manual`` / ``fan auto`` / ``fan off`` when control is disabled."""
    mode = str(fan_mode or "").strip()
    if mode == "disabled":
        return "fan off"
    if fan_pct is None:
        return f"fan {mode}".strip() or "fan"
    return f"fan {int(round(float(fan_pct)))}% {mode}".strip()


def status_line(
    *,
    temp_c=None,
    power_w=None,
    core_clock_mhz=None,
    voltage_mv=None,
    fan_pct=None,
    fan_mode="auto",
    tier=None,
) -> str:
    return format_log_line(
        "status",
        None if temp_c is None else f"{float(temp_c):.0f}C",
        None if power_w is None else f"{float(power_w):.0f}W",
        format_clock_voltage(core_clock_mhz, voltage_mv),
        format_fan_section(fan_pct, fan_mode),
        tier,
    )


def _bucket(value, size):
    if value is None:
        return None
    return round(float(value) / float(size))


def status_signature(
    *,
    temp_c=None,
    power_w=None,
    core_clock_mhz=None,
    voltage_mv=None,
    fan_pct=None,
    fan_mode="auto",
    tier=None,
) -> tuple:
    """A coarse signature of the status so steady-state jitter does not spam a
    new line every poll. Continuous values are bucketed; fan mode/tier compare
    exactly. ``status_line`` is re-emitted only when this changes."""
    return (
        _bucket(temp_c, 2.0),
        _bucket(power_w, 10.0),
        _bucket(core_clock_mhz, 30.0),
        _bucket(voltage_mv, 5.0),
        None if fan_pct is None else int(round(float(fan_pct))),
        str(fan_mode or ""),
        tier,
    )


def tier_switch_line(from_tier, to_tier, reason=None, core_clock_mhz=None, voltage_mv=None) -> str:
    return format_log_line(
        "tier",
        f"{from_tier} → {to_tier}",
        reason,
        format_clock_voltage(core_clock_mhz, voltage_mv),
    )


def fan_event_line(event, temp_c=None, fan_pct=None) -> str:
    return format_log_line(
        "fan",
        event,
        None if temp_c is None else f"{float(temp_c):.0f}C",
        None if fan_pct is None else f"{int(round(float(fan_pct)))}%",
    )


def emergency_line(event, temp_c=None, fan_pct=None, fan_mode="auto") -> str:
    return format_log_line(
        "emerg",
        event,
        None if temp_c is None else f"{float(temp_c):.0f}C",
        format_fan_section(fan_pct, fan_mode),
    )


def warn_line(what, detail=None) -> str:
    return format_log_line("warn", what, detail)


def start_line(*sections) -> str:
    return format_log_line("start", *sections)
