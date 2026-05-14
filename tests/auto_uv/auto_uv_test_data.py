from __future__ import annotations

"""Small Auto-UV3 data builders keep tests readable without hiding behavior.
Each builder returns the plain dictionaries used by the production boundary."""


def base_curve(
    start_mv: int = 800,
    stop_mv: int = 1025,
    step_mv: int = 25,
    start_mhz: int = 2000,
    step_mhz: int = 30,
) -> list[dict]:
    return [
        {
            "index": index,
            "voltage_mv": voltage_mv,
            "base_mhz": start_mhz + index * step_mhz,
            "target_mhz": start_mhz + index * step_mhz,
            "new_offset_mhz": 0,
        }
        for index, voltage_mv in enumerate(range(start_mv, stop_mv, step_mv))
    ]


def wide_base_curve() -> list[dict]:
    return base_curve(800, 1300, 5, 2200, 10)


def stable_probe_result(
    *,
    fps: float = 100.0,
    frames: int = 1000,
    clock_mhz: float = 2100.0,
    power_w: float = 180.0,
) -> dict:
    return {
        "success": True,
        "timedemo_runs": [{"frames": frames, "seconds": 10.0, "fps": fps}],
        "telemetry_samples": [
            {
                "elapsed_s": 6.0,
                "power_w": power_w,
                "core_clock_mhz": clock_mhz,
                "gpu_util_pct": 99.0,
            }
        ],
    }


def probe_summary(
    voltage_mv: int,
    *,
    clock_mhz: float,
    fps: float = 100.0,
    power_w: float = 200.0,
    temperature_c: float = 60.0,
) -> dict:
    return {
        "candidate_voltage_mv": int(voltage_mv),
        "avg_voltage_mv": float(voltage_mv),
        "avg_core_clock_mhz": float(clock_mhz),
        "avg_fps": float(fps),
        "avg_power_w": float(power_w),
        "avg_temperature_c": float(temperature_c),
        "efficiency_fps_per_w": float(fps) / float(power_w),
    }
