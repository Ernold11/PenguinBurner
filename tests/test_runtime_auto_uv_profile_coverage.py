"""Coverage-focused tests for saved_uv_profiles.runtime_auto_uv_profile.

These exercise the error paths, fallbacks, and edge inputs of the runtime
Auto-UV profile loader and memory-offset applier. The loader is driven by
monkeypatching ``resolve_auto_uv_profile`` in the module namespace so each
branch can be hit with a small on-disk JSON payload.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import saved_uv_profiles.runtime_auto_uv_profile as runtime
from penguin_burner_errors import NvmlError


def _write_profile(tmp_path, payload):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _stub_resolver(monkeypatch, path, profile_payload=None):
    """Make resolve_auto_uv_profile return (path, profile_payload)."""

    def _resolve(selector, *, allow_unverified=False):
        return (path, profile_payload if profile_payload is not None else {})

    monkeypatch.setattr(runtime, "resolve_auto_uv_profile", _resolve)


def _good_payload():
    return {
        "points": [
            {
                "index": 0,
                "voltage_mv": 800,
                "base_mhz": 1500,
                "target_mhz": 1800,
                "new_offset_mhz": 300,
            },
            {
                "index": 1,
                "voltage_mv": 900,
                "base_mhz": 1700,
                "target_mhz": 2100,
                "new_offset_mhz": 400,
            },
        ],
        "lock_clock_mhz": 1900,
        "candidate_voltage_mv": 850,
        "profile_id": "pid-1",
        "candidate_id": "cid-1",
    }


# --- load_auto_uv_final_curve: resolver outcomes ---------------------------


def test_unresolved_with_selector_raises(monkeypatch):
    monkeypatch.setattr(
        runtime, "resolve_auto_uv_profile", lambda *a, **k: None
    )
    with pytest.raises(NvmlError, match="Auto-UV profile not found: my-sel"):
        runtime.load_auto_uv_final_curve("my-sel")


def test_unresolved_without_selector_returns_none(monkeypatch):
    # Empty selector + unresolved -> latest lookup that yields nothing.
    monkeypatch.setattr(
        runtime, "resolve_auto_uv_profile", lambda *a, **k: None
    )
    assert runtime.load_auto_uv_final_curve("") is None
    assert runtime.load_auto_uv_final_curve("   ") is None


def test_missing_file_returns_none(monkeypatch, tmp_path):
    missing = tmp_path / "does_not_exist.json"
    _stub_resolver(monkeypatch, missing)
    assert runtime.load_auto_uv_final_curve("latest") is None


# --- load_auto_uv_final_curve: points discovery / validation ---------------


def test_falls_back_to_plan_key(monkeypatch, tmp_path):
    payload = _good_payload()
    payload["plan"] = payload.pop("points")  # no "points" key -> use "plan"
    path = _write_profile(tmp_path, payload)
    _stub_resolver(monkeypatch, path)
    result = runtime.load_auto_uv_final_curve("latest")
    assert len(result["plan"]) == 2


def test_no_points_raises(monkeypatch, tmp_path):
    payload = _good_payload()
    payload.pop("points")
    path = _write_profile(tmp_path, payload)
    _stub_resolver(monkeypatch, path)
    with pytest.raises(NvmlError, match="no V/F points"):
        runtime.load_auto_uv_final_curve("latest")


def test_non_object_point_raises(monkeypatch, tmp_path):
    payload = _good_payload()
    payload["points"] = ["not-a-dict"]
    path = _write_profile(tmp_path, payload)
    _stub_resolver(monkeypatch, path)
    with pytest.raises(NvmlError, match="non-object point"):
        runtime.load_auto_uv_final_curve("latest")


def test_invalid_point_fields_raises(monkeypatch, tmp_path):
    payload = _good_payload()
    # Missing required keys -> KeyError caught -> NvmlError.
    payload["points"] = [{"index": 0}]
    path = _write_profile(tmp_path, payload)
    _stub_resolver(monkeypatch, path)
    with pytest.raises(NvmlError, match="point is invalid"):
        runtime.load_auto_uv_final_curve("latest")


def test_non_numeric_point_value_raises(monkeypatch, tmp_path):
    payload = _good_payload()
    payload["points"][0]["voltage_mv"] = "not-an-int"  # ValueError path
    path = _write_profile(tmp_path, payload)
    _stub_resolver(monkeypatch, path)
    with pytest.raises(NvmlError, match="point is invalid"):
        runtime.load_auto_uv_final_curve("latest")


# --- load_auto_uv_final_curve: lock/voltage & flatten target ---------------


@pytest.mark.parametrize(
    "field", ["lock_clock_mhz", "candidate_voltage_mv"]
)
def test_missing_lock_or_voltage_raises(monkeypatch, tmp_path, field):
    payload = _good_payload()
    payload[field] = 0
    path = _write_profile(tmp_path, payload)
    _stub_resolver(monkeypatch, path)
    with pytest.raises(NvmlError, match="missing lock clock or voltage"):
        runtime.load_auto_uv_final_curve("latest")


def test_ceiling_clock_set_when_above_lock(monkeypatch, tmp_path):
    payload = _good_payload()
    # target_mhz at/above lock voltage exceeds lock_clock_mhz -> ceiling set.
    payload["lock_clock_mhz"] = 1900
    payload["candidate_voltage_mv"] = 850
    path = _write_profile(tmp_path, payload)
    _stub_resolver(monkeypatch, path)
    result = runtime.load_auto_uv_final_curve("latest")
    # Highest target at voltage >= 850 is 2100 (the 900mV point).
    assert result["flatten_target"]["ceiling_clock_mhz"] == 2100


def test_ceiling_clock_absent_when_not_above_lock(monkeypatch, tmp_path):
    payload = _good_payload()
    payload["lock_clock_mhz"] = 5000  # above all targets
    path = _write_profile(tmp_path, payload)
    _stub_resolver(monkeypatch, path)
    result = runtime.load_auto_uv_final_curve("latest")
    assert "ceiling_clock_mhz" not in result["flatten_target"]


def test_tail_rise_bins_from_nested_flatten_target(monkeypatch, tmp_path):
    payload = _good_payload()
    payload["flatten_target"] = {"tail_rise_bins": 3}
    path = _write_profile(tmp_path, payload)
    _stub_resolver(monkeypatch, path)
    result = runtime.load_auto_uv_final_curve("latest")
    assert result["flatten_target"]["tail_rise_bins"] == 3


def test_tail_rise_bins_negative_clamped_to_zero(monkeypatch, tmp_path):
    payload = _good_payload()
    payload["tail_rise_bins"] = -5  # top-level fallback, clamped to 0
    path = _write_profile(tmp_path, payload)
    _stub_resolver(monkeypatch, path)
    result = runtime.load_auto_uv_final_curve("latest")
    assert result["flatten_target"]["tail_rise_bins"] == 0


def test_tail_rise_bins_invalid_swallowed(monkeypatch, tmp_path):
    payload = _good_payload()
    payload["tail_rise_bins"] = "bogus"  # TypeError/ValueError -> pass
    path = _write_profile(tmp_path, payload)
    _stub_resolver(monkeypatch, path)
    result = runtime.load_auto_uv_final_curve("latest")
    assert "tail_rise_bins" not in result["flatten_target"]


def test_profile_and_candidate_id_fallback_to_profile_payload(
    monkeypatch, tmp_path
):
    payload = _good_payload()
    payload.pop("profile_id")
    payload.pop("candidate_id")
    path = _write_profile(tmp_path, payload)
    # profile_payload (from resolver) supplies the ids as a fallback.
    _stub_resolver(
        monkeypatch,
        path,
        profile_payload={"profile_id": "fp", "candidate_id": "fc"},
    )
    result = runtime.load_auto_uv_final_curve("latest")
    assert result["profile_id"] == "fp"
    assert result["candidate_id"] == "fc"


# --- profile_memory_offset_mhz ---------------------------------------------


def test_memory_offset_clamped_to_max():
    assert (
        runtime.profile_memory_offset_mhz({"memory_offset_mhz": 99999})
        == runtime.MAX_AFTERBURNER_MEM_OFFSET_MHZ
    )


def test_memory_offset_clamped_to_zero_floor():
    assert runtime.profile_memory_offset_mhz({"memory_offset_mhz": -50}) == 0


def test_memory_offset_rounds_float():
    assert (
        runtime.profile_memory_offset_mhz({"memory_offset_mhz": 100.6}) == 101
    )


def test_memory_offset_secondary_key():
    assert (
        runtime.profile_memory_offset_mhz({"mem_clk_vf_offset_mhz": 200}) == 200
    )


def test_memory_offset_skips_none_and_empty():
    assert (
        runtime.profile_memory_offset_mhz(
            {"memory_offset_mhz": None, "mem_clk_vf_offset_mhz": ""}
        )
        is None
    )


def test_memory_offset_invalid_returns_none():
    assert (
        runtime.profile_memory_offset_mhz({"memory_offset_mhz": "abc"}) is None
    )


def test_memory_offset_non_dict_returns_none():
    assert runtime.profile_memory_offset_mhz(None) is None
    assert runtime.profile_memory_offset_mhz("not-a-dict") is None


# --- apply_auto_uv_profile_memory_offset -----------------------------------


def test_apply_offset_none_returns_empty():
    assert (
        runtime.apply_auto_uv_profile_memory_offset(
            profile_label="L", memory_offset_mhz=None, gpu_policy_controller=None
        )
        == {}
    )


def test_apply_offset_zero_without_controller_returns_value():
    assert runtime.apply_auto_uv_profile_memory_offset(
        profile_label="L", memory_offset_mhz=0, gpu_policy_controller=None
    ) == {"mem_clk_vf_offset_mhz": 0}


def test_apply_offset_nonzero_without_controller_raises():
    with pytest.raises(NvmlError, match="Linux GPU policy helper is unavailable"):
        runtime.apply_auto_uv_profile_memory_offset(
            profile_label="L",
            memory_offset_mhz=200,
            gpu_policy_controller=None,
        )


def test_apply_offset_with_controller_success():
    calls = {}

    def _apply(*, mem_clk_vf_offset_mhz):
        calls["mhz"] = mem_clk_vf_offset_mhz

    controller = SimpleNamespace(apply_clock_offsets=_apply)
    result = runtime.apply_auto_uv_profile_memory_offset(
        profile_label="L",
        memory_offset_mhz=300,
        gpu_policy_controller=controller,
    )
    assert result == {"mem_clk_vf_offset_mhz": 300}
    assert calls["mhz"] == 300


def test_apply_offset_with_controller_failure_raises():
    def _apply(*, mem_clk_vf_offset_mhz):
        raise RuntimeError("driver said no")

    controller = SimpleNamespace(apply_clock_offsets=_apply)
    with pytest.raises(NvmlError, match="driver rejected nvmlDeviceSetMemClkVfOffset"):
        runtime.apply_auto_uv_profile_memory_offset(
            profile_label="L",
            memory_offset_mhz=300,
            gpu_policy_controller=controller,
        )
