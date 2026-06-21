from __future__ import annotations

from types import SimpleNamespace

from auto_uv.domain.user_options import AUTO_UV_DEFAULTS
from cli.effective_runtime_options import build_effective_auto_uv_runtime_options
from drivers.nvidia.nvml_gpu_policy import MAX_AFTERBURNER_MEM_OFFSET_MHZ


def _args(**overrides):
    values = {
        "auto_uv_min_voltage_mv": None,
        "auto_uv_memory_offset_mhz": None,
        "auto_uv_power_limit_w": None,
        "auto_uv_tail_rise_bins": None,
        "auto_oc_target_voltage_mv": None,
        "auto_oc_target_clock_mhz": None,
        "auto_uv_max_clock_drop_pct": None,
        "auto_uv_mode": None,
        "auto_uv_require_final_choice": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_effective_runtime_options_default_to_empty_auto_uv_options() -> None:
    assert build_effective_auto_uv_runtime_options(_args()) == {}


def test_effective_runtime_options_apply_gui_scan_options_and_clamps() -> None:
    effective = build_effective_auto_uv_runtime_options(
        _args(
            auto_uv_min_voltage_mv=850,
            auto_uv_memory_offset_mhz=99999,
            auto_uv_power_limit_w=390,
            auto_uv_tail_rise_bins=999,
            auto_oc_target_voltage_mv=925,
            auto_oc_target_clock_mhz=2670,
            auto_uv_max_clock_drop_pct=-4.0,
            auto_uv_mode="performance",
            auto_uv_require_final_choice=True,
        )
    )

    assert effective["auto_uv_min_voltage_mv"] == 850
    assert effective["auto_uv_memory_offset_mhz"] == MAX_AFTERBURNER_MEM_OFFSET_MHZ
    assert effective["auto_uv_power_limit_w"] == 390
    assert effective["auto_uv_tail_rise_bins"] == AUTO_UV_DEFAULTS.max_tail_rise_bins
    assert effective["auto_oc_target_voltage_mv"] == 925
    assert effective["auto_oc_target_clock_mhz"] == 2670
    assert effective["auto_uv_max_clock_drop_pct"] == 0.0
    assert effective["auto_uv_mode"] == "performance"
    assert effective["auto_uv_require_final_choice"] is True


def test_effective_runtime_options_balanced_mode_uses_balanced_tail_default() -> None:
    effective = build_effective_auto_uv_runtime_options(_args(auto_uv_mode="balanced"))

    assert effective["auto_uv_requested_mode"] == "balanced"
    assert effective["auto_uv_mode"] == "balanced"
    assert effective["auto_uv_tail_rise_bins"] == AUTO_UV_DEFAULTS.balanced_tail_rise_bins


def test_effective_runtime_options_balanced_mode_keeps_explicit_tail_override() -> None:
    effective = build_effective_auto_uv_runtime_options(
        _args(auto_uv_mode="balanced", auto_uv_tail_rise_bins=2)
    )

    assert effective["auto_uv_mode"] == "balanced"
    assert effective["auto_uv_tail_rise_bins"] == 2
