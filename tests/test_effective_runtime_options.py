from __future__ import annotations

from types import SimpleNamespace

from auto_uv3.auto_uv_user_options import (
    AUTO_UV_DEFAULTS,
    AUTO_UV_YOLO_MAX_CLOCK_BUMP_BUDGET_RATIO,
)
from cli.effective_runtime_options import build_effective_afterburner_runtime_options
from nvml_gpu_policy import MAX_AFTERBURNER_MEM_OFFSET_MHZ


def _args(**overrides):
    values = {
        "afterburner_dir": "",
        "profile_section": "",
        "afterburner_device_profile": "",
        "power_limit_override_w": None,
        "preserve_base_below_mv": None,
        "auto_uv_max_drop_pct": None,
        "auto_uv_final_seconds": None,
        "auto_uv_short_seconds": None,
        "auto_uv_memory_offset_mhz": None,
        "auto_uv_tail_rise_bins": None,
        "auto_uv_efficiency_stop_streak": None,
        "auto_uv_min_efficiency_stop_drop_pct": None,
        "auto_uv_max_clock_drop_pct": None,
        "auto_uv_clock_bump_budget_ratio": None,
        "yolo": False,
        "auto_uv_mode": None,
        "auto_uv_require_final_choice": False,
        "dangerously_skip_validation": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_effective_runtime_options_preserve_stored_values_without_cli_overrides() -> None:
    stored = {
        "afterburner_root": "/stored",
        "auto_uv_max_drop_pct": 16.0,
    }

    effective = build_effective_afterburner_runtime_options(_args(), stored)

    assert effective == stored
    assert effective is not stored


def test_effective_runtime_options_apply_cli_overrides_and_clamps(tmp_path) -> None:
    effective = build_effective_afterburner_runtime_options(
        _args(
            afterburner_dir=str(tmp_path),
            profile_section=" Profile1 ",
            afterburner_device_profile="GPU0.cfg",
            power_limit_override_w=0,
            preserve_base_below_mv=825,
            auto_uv_max_drop_pct=-1.0,
            auto_uv_final_seconds=0,
            auto_uv_short_seconds=999,
            auto_uv_memory_offset_mhz=99999,
            auto_uv_tail_rise_bins=999,
            auto_uv_efficiency_stop_streak=-5,
            auto_uv_min_efficiency_stop_drop_pct=-2.5,
            auto_uv_max_clock_drop_pct=-4.0,
            auto_uv_clock_bump_budget_ratio=999.0,
            yolo=True,
            auto_uv_mode="performance",
            auto_uv_require_final_choice=True,
            dangerously_skip_validation=True,
        ),
        {},
    )

    assert effective["afterburner_root"] == str(tmp_path)
    assert effective["afterburner_profile"] == "Profile1"
    assert effective["afterburner_device_profile"] == "GPU0.cfg"
    assert effective["power_limit_override_w"] is None
    assert effective["preserve_base_below_mv"] == 825
    assert effective["auto_uv_max_drop_pct"] is None
    assert effective["auto_uv_final_seconds"] is None
    assert effective["auto_uv_short_seconds"] == 60
    assert effective["auto_uv_memory_offset_mhz"] == MAX_AFTERBURNER_MEM_OFFSET_MHZ
    assert effective["auto_uv_tail_rise_bins"] == AUTO_UV_DEFAULTS.max_tail_rise_bins
    assert effective["auto_uv_efficiency_stop_streak"] == 0
    assert effective["auto_uv_min_efficiency_stop_drop_pct"] == 0.0
    assert effective["auto_uv_max_clock_drop_pct"] == 0.0
    assert (
        effective["auto_uv_clock_bump_budget_ratio"]
        == AUTO_UV_YOLO_MAX_CLOCK_BUMP_BUDGET_RATIO
    )
    assert effective["auto_uv_yolo"] is True
    assert effective["auto_uv_mode"] == "performance"
    assert effective["auto_uv_require_final_choice"] is True
    assert effective["dangerously_skip_validation"] is True
