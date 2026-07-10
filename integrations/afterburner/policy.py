"""Pure Afterburner -> Linux GPU-policy translation.

These helpers map MSI Afterburner profile settings onto the concrete power and
clock actions applied by ``DaemonGpuClient``. They
hold no NVML/ctypes state, so they live apart from the controller and stay
trivially unit-testable.
"""
from __future__ import annotations

MAX_AFTERBURNER_MEM_OFFSET_MHZ = 2000


def afterburner_offset_khz_to_mhz(offset_khz):
    if offset_khz is None:
        return None
    return int(round(int(offset_khz) / 1000.0))


def clamp_afterburner_mem_offset_mhz(offset_mhz):
    if offset_mhz is None:
        return None
    return max(
        -MAX_AFTERBURNER_MEM_OFFSET_MHZ,
        min(MAX_AFTERBURNER_MEM_OFFSET_MHZ, int(offset_mhz)),
    )


def _resolve_power_limit_cap(power_limit_cap_w, power_limits):
    if power_limit_cap_w is not None:
        cap_w = int(power_limit_cap_w)
        if cap_w > 0:
            return int(cap_w), "manual"

    return None, "none"


def translate_afterburner_power_limit_pct(
    power_limit_pct,
    *,
    power_limits,
    power_limit_cap_w=None,
):
    cap_w, cap_mode = _resolve_power_limit_cap(power_limit_cap_w, power_limits)
    if power_limit_pct is None:
        return {
            "power_limit_source_w": None,
            "power_limit_w": None,
            "power_limit_cap_w": cap_w,
            "power_limit_cap_mode": cap_mode,
        }

    default_limit_w = power_limits.get("power_limit_default_w")
    if default_limit_w is None:
        return {
            "power_limit_source_w": None,
            "power_limit_w": None,
            "power_limit_cap_w": cap_w,
            "power_limit_cap_mode": cap_mode,
        }

    target_w = int(round(float(default_limit_w) * float(power_limit_pct) / 100.0))
    min_limit_w = power_limits.get("power_limit_min_w")
    max_limit_w = power_limits.get("power_limit_max_w")
    if min_limit_w is not None:
        target_w = max(int(min_limit_w), target_w)
    if max_limit_w is not None:
        target_w = min(int(max_limit_w), target_w)
    source_w = int(target_w)

    if cap_w is not None:
        target_w = min(target_w, int(cap_w))
        if min_limit_w is not None:
            target_w = max(int(min_limit_w), target_w)
        if max_limit_w is not None:
            target_w = min(int(max_limit_w), target_w)

    return {
        "power_limit_source_w": source_w,
        "power_limit_w": int(target_w),
        "power_limit_cap_w": cap_w,
        "power_limit_cap_mode": cap_mode,
    }


def translate_afterburner_gpu_policy(
    profile_settings,
    *,
    power_limits,
    power_limit_cap_w=None,
):
    power_limit_pct = profile_settings.get("power_limit_pct")
    core_clk_boost_khz = profile_settings.get("core_clk_boost_khz")
    mem_clk_boost_khz = profile_settings.get("mem_clk_boost_khz")
    requested_mem_clk_vf_offset_mhz = afterburner_offset_khz_to_mhz(mem_clk_boost_khz)
    mem_clk_vf_offset_mhz = clamp_afterburner_mem_offset_mhz(
        requested_mem_clk_vf_offset_mhz
    )
    power_limit_translation = translate_afterburner_power_limit_pct(
        power_limit_pct,
        power_limits=power_limits,
        power_limit_cap_w=power_limit_cap_w,
    )

    return {
        "power_limit_pct": power_limit_pct,
        "power_limit_source_w": power_limit_translation["power_limit_source_w"],
        "power_limit_w": power_limit_translation["power_limit_w"],
        "power_limit_cap_w": power_limit_translation["power_limit_cap_w"],
        "power_limit_cap_mode": power_limit_translation["power_limit_cap_mode"],
        "power_limit_default_w": power_limits.get("power_limit_default_w"),
        "power_limit_min_w": power_limits.get("power_limit_min_w"),
        "power_limit_max_w": power_limits.get("power_limit_max_w"),
        "core_clk_boost_khz": core_clk_boost_khz,
        "core_clk_boost_linux_mode": "unmapped"
        if core_clk_boost_khz is not None
        else "none",
        "mem_clk_boost_khz": mem_clk_boost_khz,
        "mem_clk_vf_offset_requested_mhz": requested_mem_clk_vf_offset_mhz,
        "mem_clk_vf_offset_mhz": mem_clk_vf_offset_mhz,
        "mem_clk_vf_offset_limit_mhz": MAX_AFTERBURNER_MEM_OFFSET_MHZ,
        "thermal_limit_raw": profile_settings.get("thermal_limit_raw", ""),
        "thermal_prioritize": profile_settings.get("thermal_prioritize"),
        "fan_mode": profile_settings.get("fan_mode"),
        "fan_speed_pct": profile_settings.get("fan_speed_pct"),
        "fan_mode2": profile_settings.get("fan_mode2"),
        "fan_speed2_pct": profile_settings.get("fan_speed2_pct"),
    }


def describe_translated_gpu_policy(gpu_policy):
    if not gpu_policy:
        return "none"

    parts = []

    power_limit_pct = gpu_policy.get("power_limit_pct")
    power_limit_source_w = gpu_policy.get("power_limit_source_w")
    power_limit_w = gpu_policy.get("power_limit_w")
    power_limit_cap_w = gpu_policy.get("power_limit_cap_w")
    if (
        power_limit_pct is not None
        and power_limit_source_w is not None
        and power_limit_w is not None
        and int(power_limit_source_w) != int(power_limit_w)
    ):
        if power_limit_cap_w is not None:
            parts.append(
                f"power-limit={int(power_limit_pct)}%->{int(power_limit_source_w)}W "
                f"capped(manual)->{int(power_limit_w)}W"
            )
        else:
            parts.append(f"power-limit={int(power_limit_pct)}%->{int(power_limit_w)}W")
    elif power_limit_pct is not None and power_limit_w is not None:
        parts.append(f"power-limit={int(power_limit_pct)}%->{int(power_limit_w)}W")
    elif power_limit_pct is not None:
        parts.append(f"power-limit={int(power_limit_pct)}%")
    elif power_limit_w is not None:
        parts.append(f"power-limit={int(power_limit_w)}W")

    core_clk_boost_linux_mode = str(gpu_policy.get("core_clk_boost_linux_mode", ""))
    core_clk_boost_khz = gpu_policy.get("core_clk_boost_khz")
    if core_clk_boost_khz is not None and core_clk_boost_linux_mode == "unmapped":
        parts.append(f"core-boost={int(core_clk_boost_khz)}kHz->unmapped")

    mem_clk_boost_khz = gpu_policy.get("mem_clk_boost_khz")
    requested_mem_clk_vf_offset_mhz = gpu_policy.get("mem_clk_vf_offset_requested_mhz")
    mem_clk_vf_offset_mhz = gpu_policy.get("mem_clk_vf_offset_mhz")
    mem_clk_vf_offset_limit_mhz = gpu_policy.get("mem_clk_vf_offset_limit_mhz")
    if (
        mem_clk_boost_khz is not None
        and mem_clk_vf_offset_mhz is not None
        and requested_mem_clk_vf_offset_mhz is not None
        and int(requested_mem_clk_vf_offset_mhz) != int(mem_clk_vf_offset_mhz)
    ):
        parts.append(
            f"mem-boost={int(mem_clk_boost_khz)}kHz->"
            f"{int(requested_mem_clk_vf_offset_mhz):+d}MHz"
            f" clamped->{int(mem_clk_vf_offset_mhz):+d}MHz"
            + (
                f" (limit {int(mem_clk_vf_offset_limit_mhz):+d}MHz)"
                if mem_clk_vf_offset_limit_mhz is not None
                else ""
            )
        )
    elif mem_clk_boost_khz is not None and mem_clk_vf_offset_mhz is not None:
        parts.append(
            f"mem-boost={int(mem_clk_boost_khz)}kHz->{int(mem_clk_vf_offset_mhz):+d}MHz"
        )
    elif mem_clk_vf_offset_mhz is not None:
        parts.append(f"mem-vf-offset={int(mem_clk_vf_offset_mhz):+d}MHz")

    thermal_limit_raw = str(gpu_policy.get("thermal_limit_raw", "")).strip()
    if thermal_limit_raw:
        parts.append(f"thermal-limit=unsupported({thermal_limit_raw})")

    return ", ".join(parts) if parts else "none"


def apply_translated_gpu_policy(policy_controller, gpu_policy):
    applied = {}

    power_limit_w = gpu_policy.get("power_limit_w")
    if power_limit_w is not None:
        policy_controller.apply_power_limit_w(power_limit_w)
        applied["power_limit_w"] = int(power_limit_w)

    mem_clk_vf_offset_mhz = gpu_policy.get("mem_clk_vf_offset_mhz")
    if mem_clk_vf_offset_mhz is not None:
        applied.update(
            policy_controller.apply_clock_offsets(
                mem_clk_vf_offset_mhz=mem_clk_vf_offset_mhz,
            )
        )

    return applied
