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


def rtx_5080_20260524_high_oc_base_curve() -> list[dict]:
    points = [
        (760, 892), (765, 1035), (770, 1177), (775, 1312),
        (785, 1455), (790, 1590), (795, 1732), (800, 1875),
        (810, 1972), (815, 2002), (820, 2025), (825, 2055),
        (835, 2085), (840, 2115), (845, 2137), (850, 2167),
        (860, 2197), (865, 2220), (870, 2250), (875, 2272),
        (885, 2302), (890, 2325), (895, 2347), (900, 2377),
        (910, 2400), (915, 2422), (920, 2445), (925, 2475),
        (935, 2497), (940, 2520), (945, 2542), (950, 2565),
        (960, 2587), (965, 2602), (970, 2625), (975, 2640),
        (985, 2662), (990, 2677), (995, 2700), (1000, 2730),
        (1010, 2752), (1015, 2767), (1020, 2790), (1025, 2805),
        (1035, 2812), (1040, 2827), (1045, 2842), (1050, 2857),
        (1060, 2865), (1065, 2880), (1070, 2887), (1075, 2902),
        (1085, 2917), (1090, 2925), (1095, 2940), (1100, 2947),
        (1110, 2962), (1115, 2970), (1120, 2985), (1125, 2992),
        (1135, 3007), (1140, 3015), (1145, 3022), (1150, 3037),
        (1160, 3045), (1165, 3052), (1170, 3067), (1175, 3075),
        (1185, 3082), (1190, 3097), (1195, 3105), (1200, 3112),
        (1210, 3120), (1215, 3127), (1220, 3142), (1225, 3150),
        (1235, 3157), (1240, 3165),
    ]
    return [
        {
            "index": index,
            "voltage_mv": voltage_mv,
            "base_mhz": base_mhz,
            "target_mhz": base_mhz,
            "new_offset_mhz": 0,
        }
        for index, (voltage_mv, base_mhz) in enumerate(points)
    ]


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
