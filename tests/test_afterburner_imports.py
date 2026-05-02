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
import ui.afterburner_import as ui_app
from ui.afterburner_import import (
    afterburner_import_profile_summary as _afterburner_import_profile_summary,
    afterburner_profile_entries as _afterburner_profile_entries,
    delete_afterburner_import_config as _delete_afterburner_import_config,
    persist_afterburner_import_selection as _persist_afterburner_import_selection,
)
from ui.constants import AFTERBURNER_PROFILE_ID


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


def _write_afterburner_export(root: Path) -> Path:
    root.mkdir(exist_ok=True)
    (root / "MSIAfterburner.cfg").write_text("[Settings]\n", encoding="utf-8")
    profiles_dir = root / "Profiles"
    profiles_dir.mkdir()
    device_profile = (
        profiles_dir / "VEN_10DE&DEV_2684&SUBSYS_00000000&REV_A1&BUS_1&DEV_0&FN_0.cfg"
    )
    defaults = _vf_curve_hex(
        [
            (800.0, 1800.0, 0.0),
            (850.0, 1900.0, 0.0),
            (900.0, 2000.0, 0.0),
            (950.0, 2100.0, 0.0),
            (1000.0, 2200.0, 0.0),
            (1050.0, 2300.0, 0.0),
        ]
    )
    undervolt = _vf_curve_hex(
        [
            (800.0, 1800.0, 0.0),
            (850.0, 1900.0, 0.0),
            (900.0, 2100.0, 0.0),
            (950.0, 2100.0, 0.0),
            (1000.0, 2100.0, 0.0),
            (1050.0, 2100.0, 0.0),
        ]
    )
    device_profile.write_text(
        "\n".join(
            [
                "[Defaults]",
                f"VFCurve={defaults}",
                "[Startup]",
                f"VFCurve={defaults}",
                "[Profile1]",
                f"VFCurve={undervolt}",
                "[Profile2]",
                f"VFCurve={defaults}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return device_profile


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


def test_gui_afterburner_import_entries_list_profiles_with_status(
    tmp_path: Path,
) -> None:
    _write_afterburner_export(tmp_path)

    entries = _afterburner_profile_entries(tmp_path)

    assert [(entry["section"], entry["importable"]) for entry in entries] == [
        ("Profile1", True),
        ("Profile2", False),
    ]
    assert entries[0]["target"] == "2100 MHz 900 mV"
    assert entries[0]["target_voltage_mv"] == 900
    assert entries[0]["target_clock_mhz"] == 2100
    assert (900.0, 2100.0) in entries[0]["curve_points"]
    assert entries[0]["status"] == "Ready"
    assert "same as Defaults or Startup" in entries[1]["status"]


def test_gui_afterburner_import_persists_selected_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "afterburner"
    _write_afterburner_export(source_root)
    config_path = tmp_path / "config" / "penguin_burner.toml"
    managed_root = tmp_path / "managed-afterburner"
    monkeypatch.setattr(ui_app, "default_runtime_config_path", lambda: config_path)
    monkeypatch.setattr(ui_app, "managed_afterburner_root", lambda: managed_root)
    entry = next(
        entry
        for entry in _afterburner_profile_entries(source_root)
        if entry["importable"]
    )

    result = _persist_afterburner_import_selection(entry)

    assert result["afterburner_root"] == str(managed_root)
    assert result["section"] == "Profile1"
    assert (managed_root / result["device_profile_relative_path"]).is_file()
    rendered = config_path.read_text(encoding="utf-8")
    assert 'afterburner_profile = "Profile1"' in rendered
    assert f'afterburner_root = "{managed_root}"' in rendered


def test_gui_afterburner_import_summary_adds_profile_list_row(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "afterburner"
    _write_afterburner_export(source_root)
    config_path = tmp_path / "config" / "penguin_burner.toml"
    managed_root = tmp_path / "managed-afterburner"
    monkeypatch.setattr(ui_app, "default_runtime_config_path", lambda: config_path)
    monkeypatch.setattr(ui_app, "managed_afterburner_root", lambda: managed_root)
    entry = next(
        entry
        for entry in _afterburner_profile_entries(source_root)
        if entry["importable"]
    )
    _persist_afterburner_import_selection(entry)

    summary = _afterburner_import_profile_summary()

    assert summary is not None
    assert summary["profile_id"] == AFTERBURNER_PROFILE_ID
    assert summary["profile_source"] == "MSI Afterburner"
    assert summary["runtime_source"] == "afterburner"
    assert summary["display_name"] == "MSI Afterburner Profile1 2100 MHz 900 mV"
    assert (900.0, 2100.0) in summary["curve_points"]


def test_gui_afterburner_import_delete_clears_profile_entry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "afterburner"
    _write_afterburner_export(source_root)
    config_path = tmp_path / "config" / "penguin_burner.toml"
    managed_root = tmp_path / "managed-afterburner"
    monkeypatch.setattr(ui_app, "default_runtime_config_path", lambda: config_path)
    monkeypatch.setattr(ui_app, "managed_afterburner_root", lambda: managed_root)
    entry = next(
        entry
        for entry in _afterburner_profile_entries(source_root)
        if entry["importable"]
    )
    _persist_afterburner_import_selection(entry)

    assert _afterburner_import_profile_summary() is not None
    assert _delete_afterburner_import_config()

    assert _afterburner_import_profile_summary() is None
    rendered = config_path.read_text(encoding="utf-8")
    assert "afterburner_profile" not in rendered
    assert "afterburner_device_profile" not in rendered
    assert f'afterburner_root = "{managed_root}"' in rendered
