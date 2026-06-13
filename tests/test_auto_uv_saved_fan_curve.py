"""Unit tests for the Auto-UV saved fan-curve loader and safety validation.

These guard fan-curve safety: a saved curve that would let the GPU run too hot
(or spin fans at an unsafe temperature) must be rejected before runtime uses it.
"""
from __future__ import annotations

import dataclasses
import json

import pytest

import runtime_fan_control.auto_uv_saved_fan_curve as saved_fan_curve
from common.penguin_burner_errors import FanCurveBlockedError, NvmlError

# A curve that satisfies every safety floor: 0% at the zero-rpm temp, >=30% at the
# active/safe-load temps, >=75% at emergency, 100% at the full-speed temp.
SAFE_CURVE = [[45.0, 0.0], [60.0, 30.0], [80.0, 75.0], [90.0, 100.0]]


def test_safe_curve_passes_validation():
    # Should not raise.
    saved_fan_curve.validate_auto_uv_fan_curve_safety(SAFE_CURVE, "<test>")


def test_too_many_points_is_rejected():
    overlong = [[float(i), 0.0] for i in range(11)]
    with pytest.raises(FanCurveBlockedError, match="too many points"):
        saved_fan_curve.validate_auto_uv_fan_curve_safety(overlong, "<test>")


def test_nonzero_at_zero_rpm_temp_is_rejected():
    curve = [[45.0, 10.0], [90.0, 100.0]]
    with pytest.raises(FanCurveBlockedError, match="zero-rpm point"):
        saved_fan_curve.validate_auto_uv_fan_curve_safety(curve, "<test>")


def test_under_speed_at_active_minimum_is_rejected():
    curve = [[45.0, 0.0], [60.0, 10.0], [90.0, 100.0]]
    with pytest.raises(FanCurveBlockedError, match="active minimum"):
        saved_fan_curve.validate_auto_uv_fan_curve_safety(curve, "<test>")


def test_under_speed_at_safe_load_target_is_rejected():
    curve = [[45.0, 0.0], [60.0, 30.0], [75.0, 10.0], [90.0, 100.0]]
    with pytest.raises(FanCurveBlockedError, match="safe load target"):
        saved_fan_curve.validate_auto_uv_fan_curve_safety(curve, "<test>")


def test_under_speed_at_emergency_is_rejected():
    curve = [[45.0, 0.0], [60.0, 30.0], [80.0, 50.0], [90.0, 100.0]]
    with pytest.raises(FanCurveBlockedError, match="emergency"):
        saved_fan_curve.validate_auto_uv_fan_curve_safety(curve, "<test>")


def test_under_speed_at_full_speed_is_rejected():
    curve = [[45.0, 0.0], [60.0, 30.0], [80.0, 75.0], [90.0, 90.0]]
    with pytest.raises(FanCurveBlockedError, match="full speed"):
        saved_fan_curve.validate_auto_uv_fan_curve_safety(curve, "<test>")


def test_hardware_override_must_be_above_full_speed_point(monkeypatch):
    tuning = saved_fan_curve.AUTO_UV_FAN_TUNING
    lowered = dataclasses.replace(tuning, hardware_auto_override_temp_c=85.0)
    monkeypatch.setattr(saved_fan_curve, "AUTO_UV_FAN_TUNING", lowered)
    with pytest.raises(FanCurveBlockedError, match="hardware-auto override"):
        saved_fan_curve.validate_auto_uv_fan_curve_safety(SAFE_CURVE, "<test>")


def _use_config_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(saved_fan_curve, "default_user_config_dir", lambda: tmp_path)
    return tmp_path / "auto-uv-fan-curve.json"


def _write_payload(monkeypatch, tmp_path, payload):
    path = _use_config_dir(monkeypatch, tmp_path)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_returns_none_when_file_absent(monkeypatch, tmp_path):
    _use_config_dir(monkeypatch, tmp_path)
    assert saved_fan_curve.load_auto_uv_fan_curve({}) is None


def test_load_rejects_non_dict_payload(monkeypatch, tmp_path):
    _write_payload(monkeypatch, tmp_path, [1, 2, 3])
    with pytest.raises(NvmlError, match="payload is invalid"):
        saved_fan_curve.load_auto_uv_fan_curve({})


def test_load_rejects_blocked_curve(monkeypatch, tmp_path):
    _write_payload(
        monkeypatch,
        tmp_path,
        {"fan_curve_blocked": True, "block_reason": "too hot", "loaded_temperature_c": 80.0},
    )
    with pytest.raises(FanCurveBlockedError, match="blocked: reason=too hot"):
        saved_fan_curve.load_auto_uv_fan_curve({})


def test_load_rejects_curve_above_safety_temp(monkeypatch, tmp_path):
    _write_payload(
        monkeypatch,
        tmp_path,
        {"loaded_temperature_c": 80.0, "fan": {"curve": SAFE_CURVE}},
    )
    with pytest.raises(FanCurveBlockedError, match="above"):
        saved_fan_curve.load_auto_uv_fan_curve({})


def test_load_rejects_missing_loaded_temperature(monkeypatch, tmp_path):
    _write_payload(monkeypatch, tmp_path, {"fan": {"curve": SAFE_CURVE}})
    with pytest.raises(FanCurveBlockedError, match="missing saved final load temperature"):
        saved_fan_curve.load_auto_uv_fan_curve({})


def test_load_rejects_missing_fan_section(monkeypatch, tmp_path):
    _write_payload(monkeypatch, tmp_path, {"loaded_temperature_c": 70.0})
    with pytest.raises(NvmlError, match="no fan section"):
        saved_fan_curve.load_auto_uv_fan_curve({})


def test_load_rejects_empty_curve(monkeypatch, tmp_path):
    _write_payload(
        monkeypatch, tmp_path, {"loaded_temperature_c": 70.0, "fan": {"curve": []}}
    )
    with pytest.raises(NvmlError, match="no curve points"):
        saved_fan_curve.load_auto_uv_fan_curve({})


def test_load_rejects_malformed_point(monkeypatch, tmp_path):
    _write_payload(
        monkeypatch,
        tmp_path,
        {"loaded_temperature_c": 70.0, "fan": {"curve": [[45.0, 0.0], [60.0]]}},
    )
    with pytest.raises(NvmlError, match="point is invalid"):
        saved_fan_curve.load_auto_uv_fan_curve({})


def test_load_returns_fan_config_for_safe_curve(monkeypatch, tmp_path):
    _write_payload(
        monkeypatch,
        tmp_path,
        {
            "loaded_temperature_c": 70.0,
            "observed_fan_speed_pct": 55.0,
            "fan": {"curve": SAFE_CURVE, "curve_source_generated_at": "2026-06-13"},
        },
    )
    result = saved_fan_curve.load_auto_uv_fan_curve({"existing": "kept"})
    assert result is not None
    fan_config = result["fan_config"]
    assert fan_config["curve"] == SAFE_CURVE
    assert fan_config["curve_source"] == "auto-uv"
    assert fan_config["mode"] == "linear"
    assert fan_config["existing"] == "kept"  # base config is preserved
