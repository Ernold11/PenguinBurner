"""Coverage for the runtime daemon's pure log-line formatters."""

from __future__ import annotations

from common.runtime_log_lines import (
    emergency_line,
    fan_event_line,
    format_clock_voltage,
    format_fan_section,
    start_line,
    status_line,
    status_signature,
    tier_switch_line,
    warn_line,
)


def test_format_clock_voltage_variants() -> None:
    assert format_clock_voltage(2642, 860) == "2642MHz @ 860mV"
    assert format_clock_voltage(2642.4, None) == "2642MHz"
    assert format_clock_voltage(None, 860.6) == "861mV"
    assert format_clock_voltage(None, None) == ""


def test_format_fan_section_variants() -> None:
    assert format_fan_section(35, "manual") == "fan 35% manual"
    assert format_fan_section(None, "auto") == "fan auto"
    assert format_fan_section(0, "disabled") == "fan off"


def test_status_line_full_and_sparse() -> None:
    assert (
        status_line(
            temp_c=58.5,
            power_w=257.06,
            core_clock_mhz=2642,
            voltage_mv=860,
            fan_pct=35,
            fan_mode="manual",
            tier="Balanced",
        )
        == "status | 58C | 257W | 2642MHz @ 860mV | fan 35% manual | Balanced"
    )
    # Missing clock/voltage/tier just collapse out — no empty cells.
    assert status_line(temp_c=40, power_w=24, fan_mode="auto") == (
        "status | 40C | 24W | fan auto"
    )


def test_status_signature_buckets_jitter_but_reacts_to_real_change() -> None:
    base = dict(
        temp_c=58.4,
        power_w=257.0,
        core_clock_mhz=2642,
        voltage_mv=860,
        fan_pct=35,
        fan_mode="manual",
        tier="Balanced",
    )
    # Sub-bucket jitter -> same signature (no new line).
    assert status_signature(**base) == status_signature(**{**base, "temp_c": 58.9, "power_w": 259.0})
    # A real fan change -> different signature.
    assert status_signature(**base) != status_signature(**{**base, "fan_pct": 60})
    # A tier change -> different signature.
    assert status_signature(**base) != status_signature(**{**base, "tier": "Performance"})


def test_event_and_warn_lines() -> None:
    assert tier_switch_line("Balanced", "Performance", "fps-drop", 2642, 870) == (
        "tier   | Balanced → Performance | fps-drop | 2642MHz @ 870mV"
    )
    assert fan_event_line("→ manual", temp_c=72, fan_pct=48) == "fan    | → manual | 72C | 48%"
    assert emergency_line("override", temp_c=81, fan_pct=100, fan_mode="auto") == (
        "emerg  | override | 81C | fan 100% auto"
    )
    assert warn_line("overlay publish unavailable", "socket gone") == (
        "warn   | overlay publish unavailable | socket gone"
    )
    assert warn_line("latency telemetry unavailable") == "warn   | latency telemetry unavailable"


def test_start_line() -> None:
    assert start_line("GPU0", "persistence on", "power cap 257W") == (
        "start  | GPU0 | persistence on | power cap 257W"
    )
