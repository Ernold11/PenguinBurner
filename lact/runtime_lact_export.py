"""Export the active runtime curve into a LACT Nvidia config.

The exporter keeps Auto-UV and imported Afterburner export paths behind one function.
"""

from __future__ import annotations

from pathlib import Path

from common.penguin_burner_errors import NvmlError

from .export import (
    LactExportError,
    write_lact_nvidia_config,
    write_lact_nvidia_config_from_afterburner,
)


def export_lact_config(
    *,
    args,
    fan_config,
    gpu_index,
    afterburner_runtime_options,
    log,
) -> None:
    output_path = Path(args.export_lact_config)
    include_vf_curve = not bool(args.fan_curve_export)
    include_fan_curve = bool(args.fan_curve_export or args.silent_fan_curve)
    max_vf_offset_mhz = getattr(args, "lact_max_vf_offset_mhz", 1000)
    try:
        if args.lact_source == "auto-uv":
            written_path, warnings = write_lact_nvidia_config(
                output_path=output_path,
                gpu_id=str(args.lact_gpu_id),
                profile_selector=str(args.auto_uv_profile or ""),
                include_vf_curve=include_vf_curve,
                include_fan_curve=include_fan_curve,
                max_vf_offset_mhz=max_vf_offset_mhz,
            )
        else:
            written_path, warnings = write_lact_nvidia_config_from_afterburner(
                output_path=output_path,
                gpu_id=str(args.lact_gpu_id),
                current_fan_config=fan_config,
                gpu_index=gpu_index,
                afterburner_root=str(
                    afterburner_runtime_options.get("afterburner_root", "")
                ).strip(),
                section=afterburner_runtime_options.get("afterburner_profile") or None,
                device_profile_hint=afterburner_runtime_options.get(
                    "afterburner_device_profile"
                )
                or None,
                dangerously_skip_validation=bool(
                    afterburner_runtime_options.get("dangerously_skip_validation")
                ),
                preserve_base_below_mv=afterburner_runtime_options.get(
                    "preserve_base_below_mv"
                ),
                include_vf_curve=include_vf_curve,
                include_fan_curve=include_fan_curve,
                max_vf_offset_mhz=max_vf_offset_mhz,
            )
    except LactExportError as exc:
        raise NvmlError(str(exc)) from exc

    log(f"LACT Nvidia config written: {written_path}")
    for warning in warnings:
        log(f"LACT export warning: {warning}")
    log(
        "Apply deliberately, for example: "
        f"sudo install -m 0644 {written_path} /etc/lact/config.yaml"
    )
