from __future__ import annotations

import json

import lact.export as lact_export
from lact import build_lact_nvidia_config


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
        gpu_id="10DE:2704-1462:5110-0000:09:00.0"
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
