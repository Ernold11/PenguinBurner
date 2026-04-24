from __future__ import annotations

from pathlib import Path
import struct

from afterburner.fan_curve import (
    decode_sw_auto_fan_flags,
    load_afterburner_fan_settings,
    parse_sw_auto_fan_curve,
    speed_for_temperature,
    temperature_for_speed,
    validate_afterburner_fan_settings,
)
from afterburner.vfcurve import (
    load_afterburner_profile_settings,
    parse_vfcurve_blob,
    point_map_by_voltage,
)


def _fan_curve_hex(points: list[tuple[float, float]], *, flags: int = 0) -> str:
    payload = struct.pack("<III", 1, len(points), flags)
    for temp_c, speed_pct in points:
        payload += struct.pack("<ff", float(temp_c), float(speed_pct))
    return payload.hex()


def _vf_curve_hex(points: list[tuple[float, float, float]]) -> str:
    values = [1.0, float(len(points)), 0.0]
    for voltage_mv, frequency_mhz, third_value in points:
        values.extend([float(voltage_mv), float(frequency_mhz), float(third_value)])
    values.extend([0.0, 0.0, 0.0])
    return b"".join(struct.pack("<f", value) for value in values).hex()


def test_parse_afterburner_fan_curve_blob_and_interpolate() -> None:
    parsed = parse_sw_auto_fan_curve(
        _fan_curve_hex([(40.0, 0.0), (60.0, 30.0), (80.0, 75.0)], flags=0x5)
    )

    assert parsed["point_count"] == 3
    assert parsed["flags_u32"] == 0x5
    assert parsed["points"][1]["temperature_c"] == 60.0
    assert temperature_for_speed(parsed["points"], 15.0) == 50.0
    assert speed_for_temperature(parsed["points"], 70.0) == 52.5
    assert decode_sw_auto_fan_flags(0x5) == {
        "force_update_each_period": True,
        "override_zero_with_hardware_curve": True,
        "unknown_bits_u32": 0,
    }


def test_validate_afterburner_fan_curve_reports_human_readable_problems() -> None:
    settings = {
        "sw_auto_enabled": 0,
        "curve": {
            "points": [
                {"temperature_c": 40.0, "speed_pct": 50.0},
                {"temperature_c": 60.0, "speed_pct": 40.0},
            ]
        },
        "curve2": {
            "points": [
                {"temperature_c": 40.0, "speed_pct": 30.0},
                {"temperature_c": 60.0, "speed_pct": 60.0},
            ]
        },
    }

    result = validate_afterburner_fan_settings(settings)

    assert result["valid"] is False
    assert "Afterburner software auto fan control is disabled" in result["problems"]
    assert "primary fan curve speeds must be nondecreasing" in result["problems"]


def test_load_afterburner_fan_settings_from_synthetic_profile(tmp_path: Path) -> None:
    profile = tmp_path / "MSIAfterburner.cfg"
    primary = _fan_curve_hex([(45.0, 0.0), (60.0, 30.0), (80.0, 80.0)])
    reference = _fan_curve_hex([(45.0, 0.0), (60.0, 40.0), (80.0, 90.0)])
    profile.write_text(
        "\n".join(
            [
                "SwAutoFanControl=1",
                "SwAutoFanControlFlags=00000005h",
                "SwAutoFanControlPeriod=5000",
                f"SwAutoFanControlCurve={primary}",
                f"SwAutoFanControlCurve2={reference}",
            ]
        )
        + "\n"
    )

    settings = load_afterburner_fan_settings(profile)

    assert settings["sw_auto_enabled"] == 1
    assert settings["period_ms"] == 5000
    assert settings["flags"]["force_update_each_period"] is True
    assert settings["curve"]["points"][2]["speed_pct"] == 80.0


def test_parse_afterburner_vf_curve_blob_without_real_profile() -> None:
    header, points, tail = parse_vfcurve_blob(
        _vf_curve_hex([(800.0, 1800.0, 0.0), (900.0, 2000.0, 0.0)])
    )

    assert "magic_u32" in header
    assert tail == []
    assert point_map_by_voltage(points)[800]["frequency_mhz"] == 1800.0
    assert point_map_by_voltage(points)[900]["frequency_mhz"] == 2000.0


def test_load_afterburner_profile_settings_preserves_mixed_case_keys(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "profile.cfg"
    vf_hex = _vf_curve_hex([(800.0, 1800.0, 0.0)])
    profile.write_text(
        "\n".join(
            [
                "[Startup]",
                "Format=2",
                "PowerLimit=100",
                "CoreClkBoost=15000",
                "MemClkBoost=-5000",
                f"VFCurve={vf_hex}",
            ]
        )
        + "\n"
    )

    settings = load_afterburner_profile_settings(profile, section="startup")

    assert settings["section"] == "Startup"
    assert settings["format"] == 2
    assert settings["core_clk_boost_khz"] == 15000
    assert settings["mem_clk_boost_khz"] == -5000
    assert settings["vf_curve_hex"] == vf_hex
