"""Read and format live GPU telemetry for foreground logs.

This module keeps noisy NVML polling details away from the control loop.
"""

from __future__ import annotations

import ctypes

from .nvml_return_code import NVML_CLOCK_GRAPHICS, NVML_CLOCK_MEM, NVML_SUCCESS


def get_reported_fan_speeds(nvml, device, fan_count):
    fan_speeds = []

    for fan_idx in range(fan_count):
        speed = ctypes.c_uint()
        rc = nvml.nvmlDeviceGetFanSpeed_v2(
            device, ctypes.c_uint(fan_idx), ctypes.byref(speed)
        )
        if rc != NVML_SUCCESS:
            fan_speeds = []
            break
        fan_speeds.append(int(speed.value))

    if fan_speeds:
        return fan_speeds

    if fan_count == 1:
        speed = ctypes.c_uint()
        rc = nvml.nvmlDeviceGetFanSpeed(device, ctypes.byref(speed))
        if rc == NVML_SUCCESS:
            return [int(speed.value)]

    return None


def get_power_draw_w(nvml, device):
    power_mw = ctypes.c_uint()
    rc = nvml.nvmlDeviceGetPowerUsage(device, ctypes.byref(power_mw))
    if rc != NVML_SUCCESS:
        return None
    return power_mw.value / 1000.0


class _NvmlUtilization(ctypes.Structure):
    # nvmlUtilization_t: gpu = % of the sample period a kernel ran, memory =
    # % of the period the memory controller was busy.
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


def get_gpu_utilization_pct(nvml, device):
    util = _NvmlUtilization()
    rc = nvml.nvmlDeviceGetUtilizationRates(device, ctypes.byref(util))
    if rc != NVML_SUCCESS:
        return None
    return int(util.gpu)


def get_core_clock_mhz(nvml, device):
    clock_mhz = ctypes.c_uint()
    rc = nvml.nvmlDeviceGetClockInfo(
        device, ctypes.c_uint(NVML_CLOCK_GRAPHICS), ctypes.byref(clock_mhz)
    )
    if rc != NVML_SUCCESS:
        return None
    return int(clock_mhz.value)


def get_memory_clock_mhz(nvml, device):
    clock_mhz = ctypes.c_uint()
    rc = nvml.nvmlDeviceGetClockInfo(
        device, ctypes.c_uint(NVML_CLOCK_MEM), ctypes.byref(clock_mhz)
    )
    if rc != NVML_SUCCESS:
        return None
    return int(clock_mhz.value)


def format_vf_curve_comparison(vf_curve_reader, core_clock_mhz, voltage_uv):
    point = vf_curve_reader.find_nearest_point(core_clock_mhz, voltage_uv)
    if point is None:
        return ""

    point_freq_mhz = int(point["freq_khz"] // 1000)
    point_voltage_mv = int(point["voltage_uv"] // 1000)
    point_offset_mhz = int(point["current_offset_khz"] // 1000)

    base_point = min(
        vf_curve_reader.editable_core_points(),
        key=lambda candidate: abs(
            int(candidate["base_freq_khz"]) - int(core_clock_mhz) * 1000
        ),
    )
    base_clock_mhz = int(base_point["base_freq_khz"] // 1000)
    base_voltage_mv = int(base_point["voltage_uv"] // 1000)
    uv_delta_mv = int(point_voltage_mv - base_voltage_mv)

    return (
        f"vf_point={point_freq_mhz}MHz@{point_voltage_mv}mV "
        f"vf_offset={point_offset_mhz:+d}MHz "
        f"vf_base={base_clock_mhz}MHz@{base_voltage_mv}mV "
        f"uv={uv_delta_mv:+d}mV "
    )


def format_clock_offsets(gpu_policy_controller):
    if gpu_policy_controller is None:
        return ""

    try:
        offsets = gpu_policy_controller.get_clock_offsets()
    except Exception:
        return ""

    mem_clk_vf_offset_mhz = offsets.get("mem_clk_vf_offset_mhz")
    if mem_clk_vf_offset_mhz is None:
        return ""
    return f"mem_vf_offset={int(mem_clk_vf_offset_mhz):+d}MHz "


def format_clock_ceiling_state(clock_ceiling_controller):
    if clock_ceiling_controller is None:
        return ""
    return clock_ceiling_controller.telemetry_text()


def format_telemetry(
    nvml,
    device,
    fan_count,
    current_temp_c,
    voltage_reader=None,
    vf_curve_reader=None,
    gpu_policy_controller=None,
    power_draw_w=None,
    clock_ceiling_controller=None,
):
    reported_fan_speeds = get_reported_fan_speeds(nvml, device, fan_count)
    if reported_fan_speeds is None:
        fan_text = "n/a"
    else:
        fan_text = "/".join(f"{speed}%" for speed in reported_fan_speeds)

    if power_draw_w is None:
        power_draw_w = get_power_draw_w(nvml, device)
    power_text = "n/a" if power_draw_w is None else f"{power_draw_w:.2f}W"

    core_clock_mhz = get_core_clock_mhz(nvml, device)
    clock_text = "n/a" if core_clock_mhz is None else f"{core_clock_mhz}MHz"
    memory_clock_mhz = get_memory_clock_mhz(nvml, device)
    mem_clock_text = "n/a" if memory_clock_mhz is None else f"{memory_clock_mhz}MHz"

    voltage_uv = None
    if voltage_reader is not None:
        try:
            voltage_uv = voltage_reader.read_microvolts(device)
        except Exception:
            voltage_uv = None
    voltage_text = "n/a" if voltage_uv is None else f"{voltage_uv / 1000.0:.0f}mV"

    clock_offset_text = format_clock_offsets(gpu_policy_controller)
    clock_ceiling_text = format_clock_ceiling_state(clock_ceiling_controller)
    vf_point_text = ""
    if (
        vf_curve_reader is not None
        and core_clock_mhz is not None
        and voltage_uv is not None
    ):
        try:
            vf_curve_reader.refresh_points()
        except Exception:
            pass
        vf_point_text = format_vf_curve_comparison(
            vf_curve_reader,
            core_clock_mhz,
            voltage_uv,
        )

    return (
        f"temp={current_temp_c:.1f}C "
        f"fan={fan_text} "
        f"power={power_text} "
        f"gpu_clock={clock_text} "
        f"mem_clock={mem_clock_text} "
        f"voltage={voltage_text} "
        f"{clock_ceiling_text}"
        f"{clock_offset_text}"
        f"{vf_point_text}"
    ).rstrip()
