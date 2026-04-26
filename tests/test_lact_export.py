from __future__ import annotations

import json

import lact.export as lact_export
from lact import build_lact_nvidia_config, build_lact_nvidia_config_from_plan


def test_lact_nvidia_export_writes_full_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(lact_export, "default_user_config_dir", lambda: tmp_path)
    (tmp_path / "auto-uv-final-curve.json").write_text(
        json.dumps(
            {
                "lock_clock_mhz": 2445,
                "candidate_voltage_mv": 900,
                "points": [
                    {
                        "index": 42,
                        "voltage_mv": 900,
                        "base_mhz": 2350,
                        "target_mhz": 2445,
                        "new_offset_mhz": 95,
                    },
                    {
                        "index": 43,
                        "voltage_mv": 925,
                        "base_mhz": 2380,
                        "target_mhz": 2445,
                        "new_offset_mhz": 65,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "auto-uv-fan-curve.json").write_text(
        json.dumps(
            {
                "zero_rpm_until_temperature_c": 45.0,
                "fan": {
                    "poll_interval_s": 1.0,
                    "hysteresis_c": 2.0,
                    "curve": [[45.0, 0.0], [55.0, 30.0], [75.0, 70.0]],
                },
            }
        ),
        encoding="utf-8",
    )

    rendered, warnings = build_lact_nvidia_config(
        gpu_id="10DE:2704-1462:5110-0000:09:00.0",
        include_fan_curve=True,
    )

    assert warnings == []
    assert "daemon:\n  log_level: info\n  disable_nvapi: false" in rendered
    assert "10DE:2704-1462:5110-0000:09:00.0:" in rendered
    assert "fan_control_enabled: true" in rendered
    assert "45: 0" in rendered
    assert "55: 0.3" in rendered
    assert "auto_threshold: 45" in rendered
    assert "change_threshold: 2" in rendered
    assert "gpu_vf_curve:" in rendered
    assert "42:\n        clockspeed: 2445\n        voltage: 900" in rendered
    assert "43:\n        clockspeed: 2445\n        voltage: 925" in rendered
    assert "profiles: {}" in rendered


def test_lact_nvidia_export_defaults_to_vf_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(lact_export, "default_user_config_dir", lambda: tmp_path)
    (tmp_path / "auto-uv-final-curve.json").write_text(
        json.dumps(
            {
                "points": [
                    {
                        "index": 1,
                        "voltage_mv": 900,
                        "base_mhz": 2000,
                        "target_mhz": 2100,
                        "new_offset_mhz": 100,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "auto-uv-fan-curve.json").write_text(
        json.dumps(
            {
                "zero_rpm_until_temperature_c": 42.0,
                "fan": {
                    "poll_interval_s": 2.0,
                    "hysteresis_c": 1.0,
                    "curve": [[42.0, 0.0], [65.0, 55.0]],
                },
            }
        ),
        encoding="utf-8",
    )

    rendered, warnings = build_lact_nvidia_config(gpu_id="gpu0")

    assert warnings == []
    assert "fan_control_enabled: false" in rendered
    assert "gpu_vf_curve:" in rendered
    assert "65: 0.55" not in rendered


def test_lact_nvidia_export_disables_fan_when_no_fan_artifact(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(lact_export, "default_user_config_dir", lambda: tmp_path)
    (tmp_path / "auto-uv-final-curve.json").write_text(
        json.dumps(
            {
                "points": [
                    {
                        "index": 1,
                        "voltage_mv": 900,
                        "base_mhz": 2000,
                        "target_mhz": 2100,
                        "new_offset_mhz": 100,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    rendered, warnings = build_lact_nvidia_config(gpu_id="gpu0")

    assert warnings == []
    assert "fan_control_enabled: false" in rendered
    assert "gpu_vf_curve:" in rendered


def test_lact_nvidia_export_can_emit_fan_only_auto_uv_config(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(lact_export, "default_user_config_dir", lambda: tmp_path)
    (tmp_path / "auto-uv-fan-curve.json").write_text(
        json.dumps(
            {
                "zero_rpm_until_temperature_c": 42.0,
                "fan": {
                    "poll_interval_s": 2.0,
                    "hysteresis_c": 1.0,
                    "curve": [[42.0, 0.0], [65.0, 55.0], [82.0, 90.0]],
                },
            }
        ),
        encoding="utf-8",
    )

    rendered, warnings = build_lact_nvidia_config(
        gpu_id="gpu0",
        include_vf_curve=False,
        include_fan_curve=True,
    )

    assert warnings == []
    assert "# Source V/F curve: omitted" in rendered
    assert "fan_control_enabled: true" in rendered
    assert "interval_ms: 2000" in rendered
    assert "65: 0.55" in rendered
    assert "gpu_vf_curve:" not in rendered


def test_lact_nvidia_export_supports_afterburner_plan_source() -> None:
    rendered, warnings = build_lact_nvidia_config_from_plan(
        gpu_id="gpu0",
        vf_plan=[
            {
                "index": 7,
                "voltage_mv": 950,
                "base_mhz": 2400,
                "target_mhz": 2550,
                "new_offset_mhz": 150,
            }
        ],
        fan_config={
            "poll_interval_s": 5.0,
            "hysteresis_c": 0.0,
            "auto_restore_temp_c": 45.0,
            "curve": [[45.0, 0.0], [60.0, 50.0], [80.0, 100.0]],
        },
        source_label="afterburner",
        source_vf="/mnt/windows/MSI Afterburner/Profiles/GPU.cfg [Profile1]",
        source_fan="/mnt/windows/MSI Afterburner/MSIAfterburner.cfg",
        include_fan_curve=True,
    )

    assert warnings == []
    assert "# Source: afterburner" in rendered
    assert "interval_ms: 5000" in rendered
    assert "60: 0.5" in rendered
    assert "7:\n        clockspeed: 2550\n        voltage: 950" in rendered


def test_lact_nvidia_export_can_emit_fan_only_plan_config() -> None:
    rendered, warnings = build_lact_nvidia_config_from_plan(
        gpu_id="gpu0",
        vf_plan=None,
        fan_config={
            "poll_interval_s": 5.0,
            "hysteresis_c": 0.0,
            "auto_restore_temp_c": 45.0,
            "curve": [[45.0, 0.0], [60.0, 50.0], [80.0, 100.0]],
        },
        source_label="afterburner",
        source_vf="omitted",
        source_fan="/mnt/windows/MSI Afterburner/MSIAfterburner.cfg",
        include_vf_curve=False,
        include_fan_curve=True,
    )

    assert warnings == []
    assert "# Source: afterburner" in rendered
    assert "# Source V/F curve: omitted" in rendered
    assert "fan_control_enabled: true" in rendered
    assert "gpu_vf_curve:" not in rendered
