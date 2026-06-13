#!/usr/bin/env python3

from pathlib import Path

from afterburner.fan_curve import (
    load_afterburner_fan_settings,
    resolve_afterburner_fan_profile,
)
from afterburner.import_fan_curve import build_imported_fan_section
from afterburner.import_vf_curve import build_plan
from afterburner.vfcurve import (
    load_afterburner_profile_settings,
    resolve_afterburner_vf_source,
)
from afterburner.vfcurve_describe import (
    describe_afterburner_flatten_validation,
    describe_afterburner_profile_settings,
    describe_afterburner_vfcurve_analysis,
)
from common.ascii_chart import render_line_chart
from nvidia_driver.hidden_nvapi_vf import create_hidden_vf_curve_reader
from nvidia_driver.nvml_gpu_policy import (
    NvmlGpuPolicyController,
    describe_translated_gpu_policy,
    translate_afterburner_gpu_policy,
)
from common.penguin_burner_paths import resolve_afterburner_root
from runtime_debug import (
    debug_exception,
    debug_log,
    emit_afterburner_debug_snapshot,
    log,
)


def format_dry_run_power_summary(translated_gpu_policy):
    parts = []

    power_limit_pct = translated_gpu_policy.get("power_limit_pct")
    source_w = translated_gpu_policy.get("power_limit_source_w")
    target_w = translated_gpu_policy.get("power_limit_w")
    cap_w = translated_gpu_policy.get("power_limit_cap_w")
    if power_limit_pct is not None and target_w is not None:
        power_text = f"AB {int(power_limit_pct)}% -> {int(target_w)}W"
        if (
            source_w is not None
            and int(source_w) != int(target_w)
            and cap_w is not None
        ):
            power_text += f" (manual cap, uncapped {int(source_w)}W)"
        parts.append(power_text)

    mem_offset_mhz = translated_gpu_policy.get("mem_clk_vf_offset_mhz")
    if mem_offset_mhz is not None:
        parts.append(f"memory target {int(mem_offset_mhz):+d}MHz")

    return ", ".join(parts) if parts else "none"


def format_dry_run_linux_state_summary(
    *, vf_changed_points, power_limits, clock_offsets
):
    parts = []

    current_limit_w = power_limits.get("power_limit_w")
    if current_limit_w is not None:
        parts.append(f"current power {int(current_limit_w)}W")

    mem_offset_mhz = clock_offsets.get("mem_clk_vf_offset_mhz")
    if mem_offset_mhz is not None:
        parts.append(f"current memory {int(mem_offset_mhz):+d}MHz")

    if vf_changed_points is not None:
        parts.append(f"VF points changing {int(vf_changed_points)}")

    return ", ".join(parts) if parts else "none"


def run_afterburner_dry_run(
    *,
    config_path,
    fan_config,
    gpu_index,
    afterburner_runtime_options,
):
    afterburner_root = str(
        afterburner_runtime_options.get("afterburner_root", "")
    ).strip()
    afterburner_profile = str(
        afterburner_runtime_options.get("afterburner_profile", "")
    ).strip()
    afterburner_device_profile = str(
        afterburner_runtime_options.get("afterburner_device_profile", "")
    ).strip()
    power_limit_override_w = afterburner_runtime_options.get("power_limit_override_w")
    preserve_base_below_mv = afterburner_runtime_options.get(
        "preserve_base_below_mv",
        afterburner_runtime_options.get("preserve_vanilla_below_mv"),
    )
    dangerously_skip_validation = bool(
        afterburner_runtime_options.get("dangerously_skip_validation")
    )

    if not afterburner_root:
        raise RuntimeError(
            "--dry-run requires --afterburner-dir or a configured afterburner_root in the runtime config"
        )

    afterburner_root = str(resolve_afterburner_root(afterburner_root))
    debug_log(
        f"dry-run-start gpu-index={gpu_index} config-path={config_path} "
        f"resolved-root={afterburner_root}"
    )
    emit_afterburner_debug_snapshot(
        afterburner_root=afterburner_root,
        requested_section=afterburner_profile,
        device_profile_hint=afterburner_device_profile,
        dangerously_skip_validation=dangerously_skip_validation,
    )

    policy_controller = None
    vf_curve_reader = None
    try:
        source = resolve_afterburner_vf_source(
            afterburner_root=afterburner_root,
            section=afterburner_profile or None,
            device_profile_hint=afterburner_device_profile or None,
            dangerously_skip_validation=dangerously_skip_validation,
        )
        debug_log(
            "dry-run-selected-source="
            f"profile={source['profile_path']} "
            f"section={source['section']} "
            f"skip-validation={source.get('dangerously_skip_validation')}"
        )
        section_info = source["section_info"]
        profile_settings = load_afterburner_profile_settings(
            profile_path=source["profile_path"],
            section=source["section"],
        )
        debug_log(
            "dry-run-selected-settings="
            f"{describe_afterburner_profile_settings(profile_settings)}"
        )
        flatten_target = section_info.get("flatten_target")

        fan_settings = load_afterburner_fan_settings(
            resolve_afterburner_fan_profile(afterburner_root=afterburner_root)
        )
        fan_settings["afterburner_root"] = Path(afterburner_root).expanduser()
        debug_log(
            "dry-run-fan-settings="
            f"period-ms={fan_settings['period_ms']} "
            f"flags=0x{int(fan_settings['flags_u32']):08x} "
            f"curve-points={len(fan_settings['curve']['points'])} "
            f"reference-points={len(fan_settings['curve2']['points'])}"
        )

        imported_fan_config = None
        imported_fan_error = None
        try:
            imported_fan_config = build_imported_fan_section(
                fan_config,
                fan_settings,
                gpu_index=gpu_index,
            )
        except SystemExit as exc:
            imported_fan_error = str(exc)
            debug_exception("failed to translate the imported fan curve", exc)

        power_limits = {}
        clock_offsets = {}
        policy_error = None
        translated_gpu_policy = translate_afterburner_gpu_policy(
            profile_settings,
            power_limits=power_limits,
            power_limit_cap_w=power_limit_override_w,
        )
        try:
            policy_controller = NvmlGpuPolicyController(gpu_index=gpu_index)
        except Exception as exc:
            policy_error = str(exc)
            debug_exception("failed to create the NVML GPU policy helper", exc)
        else:
            power_limits = policy_controller.query_power_limits()
            clock_offsets = policy_controller.get_clock_offsets()
            translated_gpu_policy = translate_afterburner_gpu_policy(
                profile_settings,
                power_limits=power_limits,
                power_limit_cap_w=power_limit_override_w,
            )
            debug_log(
                "dry-run-translated-policy="
                f"{describe_translated_gpu_policy(translated_gpu_policy)}"
            )

        vf_summary = None
        vf_plan = []
        missing_voltage_bins = []
        vf_curve_reader = create_hidden_vf_curve_reader(gpu_index=gpu_index)
        if vf_curve_reader is not None:
            vf_summary = vf_curve_reader.summary()
            debug_log(
                "linux-vf-summary="
                f"active-points={vf_summary['active_points']} "
                f"editable-core-points={vf_summary['editable_core_points']}"
            )
            vf_plan, missing_voltage_bins = build_plan(
                vf_curve_reader,
                section_info["materialization"]["points"],
                preserve_base_below_mv=preserve_base_below_mv,
            )
            debug_log(
                f"linux-vf-plan matched={len(vf_plan)} "
                f"missing-voltage-bins={len(missing_voltage_bins)}"
            )
        else:
            debug_log("linux-vf-summary=unavailable")

        changed_points = [
            item
            for item in vf_plan
            if int(item["current_offset_mhz"]) != int(item["new_offset_mhz"])
        ]
        if vf_plan:
            debug_log(
                f"linux-vf-point-detail count={len(vf_plan)} changed={len(changed_points)}"
            )
            for item in vf_plan:
                debug_log(
                    "linux-vf-point "
                    f"idx={int(item['index'])} "
                    f"mv={int(item['voltage_mv'])} "
                    f"base={int(item['base_mhz'])}MHz "
                    f"target={int(item['target_mhz'])}MHz "
                    f"current-offset={int(item['current_offset_mhz']):+d}MHz "
                    f"new-offset={int(item['new_offset_mhz']):+d}MHz "
                    f"preserve-base={'yes' if item.get('preserve_base') else 'no'}"
                )

        flags = fan_settings["flags"]
        lock_voltage_mv = (
            flatten_target.get("lock_voltage_mv") if flatten_target else None
        )
        end_voltage_mv = (
            flatten_target.get("end_voltage_mv") if flatten_target else None
        )
        lock_clock_mhz = (
            flatten_target.get("lock_clock_mhz") if flatten_target else None
        )
        source_label = f"{source['section']} in {source['profile_path'].name}"
        log(f"Dry run: {source_label}")
        log(f"Power and offsets: {format_dry_run_power_summary(translated_gpu_policy)}")
        if source.get("dangerously_skip_validation"):
            log(
                "Validation override: enabled. Skipping the usual flat-tail and "
                "undervolt checks against Defaults/Startup for profile selection."
            )
        if (
            flatten_target
            and lock_voltage_mv is not None
            and end_voltage_mv is not None
            and lock_clock_mhz is not None
        ):
            log(
                f"VF target: flat at {int(lock_clock_mhz)}MHz from "
                f"{int(lock_voltage_mv)}mV to {int(end_voltage_mv)}mV"
            )
        else:
            log(
                "VF target: "
                f"{describe_afterburner_vfcurve_analysis(section_info['analysis'])}"
            )
        if preserve_base_below_mv is not None:
            log(
                "VF preserve: "
                f"keep the base curve at and below {int(preserve_base_below_mv)}mV"
            )

        validation = section_info.get("flatten_validation")
        if validation and validation.get("valid"):
            log(
                "Undervolt check: "
                f"{int(validation['selected_clock_mhz'])}MHz at "
                f"{int(validation['selected_voltage_mv'])}mV is "
                f"{int(round(float(validation['undervolt_margin_mv'])))}mV below "
                f"{validation['baseline_section']} at the same clock"
            )
        elif validation:
            if source.get("dangerously_skip_validation"):
                log(
                    "Undervolt check: skipped by user request; "
                    f"{describe_afterburner_flatten_validation(validation)}"
                )
            else:
                log(
                    f"Undervolt check: {describe_afterburner_flatten_validation(validation)}"
                )

        if imported_fan_config is None:
            log(f"Fan behavior: unavailable ({imported_fan_error})")
        else:
            fan_behavior_parts = [
                f"{float(fan_settings['period_ms']) / 1000.0:.1f}s updates",
                (
                    f"takeover {float(imported_fan_config['curve_manual_takeover_temp_c']):.0f}C"
                ),
                (
                    f"restore {float(imported_fan_config['curve_auto_restore_temp_c']):.0f}C"
                ),
                (
                    "emergency "
                    f"{float(imported_fan_config['emergency_auto_override_temp_c']):.0f}C/"
                    f"{float(imported_fan_config['emergency_auto_resume_temp_c']):.0f}C"
                ),
            ]
            if flags["override_zero_with_hardware_curve"]:
                fan_behavior_parts.append("zero-RPM preserved")
            log("Fan behavior: " + ", ".join(fan_behavior_parts))

        if policy_controller is None and vf_summary is None:
            log("Linux readback: unavailable")
        else:
            log(
                "Linux readback: "
                + format_dry_run_linux_state_summary(
                    vf_changed_points=len(changed_points)
                    if vf_summary is not None
                    else None,
                    power_limits=power_limits,
                    clock_offsets=clock_offsets,
                )
            )
            if policy_controller is None and policy_error:
                log(f"Linux power/memory readback note: {policy_error}")
            if vf_summary is None:
                log("Linux VF readback note: hidden NVAPI VF helper is unavailable")
                if preserve_base_below_mv is not None:
                    log(
                        "Linux VF preserve note: "
                        "preserve-below-voltage needs Linux VF point data for an exact target preview"
                    )
            elif missing_voltage_bins:
                preview = ", ".join(
                    str(int(voltage_mv)) + "mV"
                    for voltage_mv in missing_voltage_bins[:8]
                )
                if len(missing_voltage_bins) > 8:
                    preview += ", ..."
                log(f"Unmatched voltage bins: {preview}")

        if vf_summary is not None and vf_plan:
            vf_series = [
                {
                    "name": "base",
                    "char": ".",
                    "points": sorted(
                        [
                            (
                                float(item["voltage_mv"]),
                                float(item["base_mhz"]),
                            )
                            for item in vf_plan
                        ]
                    ),
                },
                {
                    "name": "target",
                    "char": "#",
                    "points": sorted(
                        [
                            (float(item["voltage_mv"]), float(item["target_mhz"]))
                            for item in vf_plan
                        ]
                    ),
                },
            ]
            vf_title = "VF curve (target=# base=. lock=@, x=mV y=MHz)"
        else:
            vf_series = [
                {
                    "name": "target",
                    "char": "#",
                    "points": sorted(
                        [
                            (
                                float(point["voltage_mv"]),
                                float(point["frequency_mhz"]),
                            )
                            for point in section_info["materialization"]["points"]
                        ]
                    ),
                }
            ]
            vf_title = "VF curve (target=# lock=@, x=mV y=MHz)"

        print(flush=True)
        for line in render_line_chart(
            vf_title,
            series=vf_series,
            x_label="mV",
            y_label="MHz",
            x_rounding=50,
            y_rounding=100,
            highlights=(
                [
                    {
                        "x": float(flatten_target["lock_voltage_mv"]),
                        "y": float(flatten_target["lock_clock_mhz"]),
                        "char": "@",
                    }
                ]
                if flatten_target
                and flatten_target.get("lock_voltage_mv") is not None
                and flatten_target.get("lock_clock_mhz") is not None
                else []
            ),
        ):
            log(line)

        fan_series = [
            {
                "name": "primary",
                "char": "#",
                "points": sorted(
                    [
                        (
                            float(point["temperature_c"]),
                            float(point["speed_pct"]),
                        )
                        for point in fan_settings["curve"]["points"]
                    ]
                ),
            },
            {
                "name": "reference",
                "char": ".",
                "points": sorted(
                    [
                        (
                            float(point["temperature_c"]),
                            float(point["speed_pct"]),
                        )
                        for point in fan_settings["curve2"]["points"]
                    ]
                ),
            },
        ]
        print(flush=True)
        for line in render_line_chart(
            "Fan curve (primary=# reference=., x=C y=%)",
            series=fan_series,
            x_label="C",
            y_label="%",
            x_rounding=10,
            y_rounding=10,
            include_zero_y=True,
        ):
            log(line)
    except Exception as exc:
        debug_exception("dry run failed", exc)
        raise
    finally:
        if vf_curve_reader is not None:
            vf_curve_reader.close()
        if policy_controller is not None:
            policy_controller.close()
