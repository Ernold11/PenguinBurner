"""Build effective runtime options from persisted config and CLI overrides."""

from __future__ import annotations

from auto_uv3.auto_uv_user_options import (
    AUTO_UV_DEFAULTS,
    AUTO_UV_MAX_CLOCK_BUMP_BUDGET_RATIO,
    AUTO_UV_YOLO_MAX_CLOCK_BUMP_BUDGET_RATIO,
)
from auto_uv3.scan_mode import normalize_auto_uv_mode
from nvml_gpu_policy import MAX_AFTERBURNER_MEM_OFFSET_MHZ
from penguin_burner_paths import resolve_afterburner_root


def build_effective_afterburner_runtime_options(args, stored_options: dict) -> dict:
    runtime_options = dict(stored_options)
    if args.afterburner_dir.strip():
        runtime_options["afterburner_root"] = str(
            resolve_afterburner_root(args.afterburner_dir)
        )
    if args.profile_section.strip():
        runtime_options["afterburner_profile"] = str(args.profile_section).strip()
    if args.afterburner_device_profile.strip():
        runtime_options["afterburner_device_profile"] = str(
            args.afterburner_device_profile
        ).strip()
    if args.power_limit_override_w is not None:
        runtime_options["power_limit_override_w"] = (
            int(args.power_limit_override_w)
            if int(args.power_limit_override_w) > 0
            else None
        )
    if args.preserve_base_below_mv is not None:
        runtime_options["preserve_base_below_mv"] = (
            int(args.preserve_base_below_mv)
            if int(args.preserve_base_below_mv) > 0
            else None
        )
    if args.auto_uv_max_drop_pct is not None:
        runtime_options["auto_uv_max_drop_pct"] = (
            float(args.auto_uv_max_drop_pct)
            if float(args.auto_uv_max_drop_pct) > 0.0
            else None
        )
    if args.auto_uv_final_seconds is not None:
        runtime_options["auto_uv_final_seconds"] = (
            int(args.auto_uv_final_seconds)
            if int(args.auto_uv_final_seconds) > 0
            else None
        )
    if args.auto_uv_short_seconds is not None:
        runtime_options["auto_uv_short_seconds"] = (
            max(10, min(60, int(args.auto_uv_short_seconds)))
            if int(args.auto_uv_short_seconds) > 0
            else None
        )
    if args.auto_uv_memory_offset_mhz is not None:
        runtime_options["auto_uv_memory_offset_mhz"] = max(
            0,
            min(MAX_AFTERBURNER_MEM_OFFSET_MHZ, int(args.auto_uv_memory_offset_mhz)),
        )
    auto_uv_tail_rise_bins = getattr(args, "auto_uv_tail_rise_bins", None)
    if auto_uv_tail_rise_bins is not None:
        runtime_options["auto_uv_tail_rise_bins"] = max(
            0,
            min(
                int(AUTO_UV_DEFAULTS.max_tail_rise_bins),
                int(auto_uv_tail_rise_bins),
            ),
        )
    if args.auto_uv_efficiency_stop_streak is not None:
        runtime_options["auto_uv_efficiency_stop_streak"] = max(
            0,
            int(args.auto_uv_efficiency_stop_streak),
        )
    if args.auto_uv_min_efficiency_stop_drop_pct is not None:
        runtime_options["auto_uv_min_efficiency_stop_drop_pct"] = max(
            0.0,
            float(args.auto_uv_min_efficiency_stop_drop_pct),
        )
    if args.auto_uv_max_clock_drop_pct is not None:
        runtime_options["auto_uv_max_clock_drop_pct"] = max(
            0.0,
            float(args.auto_uv_max_clock_drop_pct),
        )
    if args.auto_uv_clock_bump_budget_ratio is not None:
        max_clock_bump_budget_ratio = (
            AUTO_UV_YOLO_MAX_CLOCK_BUMP_BUDGET_RATIO
            if bool(args.yolo)
            else AUTO_UV_MAX_CLOCK_BUMP_BUDGET_RATIO
        )
        runtime_options["auto_uv_clock_bump_budget_ratio"] = max(
            0.0,
            min(
                float(max_clock_bump_budget_ratio),
                float(args.auto_uv_clock_bump_budget_ratio),
            ),
        )
    if args.yolo:
        runtime_options["auto_uv_yolo"] = True
    if args.auto_uv_mode is not None:
        runtime_options["auto_uv_mode"] = normalize_auto_uv_mode(args.auto_uv_mode)
    if args.auto_uv_require_final_choice:
        runtime_options["auto_uv_require_final_choice"] = True
    if args.dangerously_skip_validation:
        runtime_options["dangerously_skip_validation"] = True
    return runtime_options
