"""Persist each candidate that passed the short stability probe.

The latest file feeds resume handoff and the candidate list feeds the final-choice UI.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

from .auto_uv_persisted_json_files import safe_json_write, verified_candidates_path
from .auto_uv_persisted_json_files import auto_uv_user_config_dir
from ..auto_uv_types import AutoUvProbeSummary
from ..curve.base_vf_curve import read_base_vf_points
from ..curve.vf_curve_flattening import build_flatten_target_for_plan


def write_latest_verified_candidate(
    *,
    plan: list[dict],
    lock_clock_mhz: int,
    voltage_mv: int,
    probe: AutoUvProbeSummary | None,
    base_probe: AutoUvProbeSummary | None = None,
    tail_rise_bins: int = 0,
) -> Path:
    output_path = auto_uv_user_config_dir() / "uv-result" / "auto-uv-latest-verified.json"
    payload = verified_candidate_payload(
        plan=plan,
        lock_clock_mhz=int(lock_clock_mhz),
        voltage_mv=int(voltage_mv),
        probe=probe,
        base_probe=base_probe,
        reason="latest-verified",
        label="passed-short-probe",
        tail_rise_bins=int(tail_rise_bins),
    )
    path = safe_json_write(output_path, payload)
    append_verified_candidate(payload)
    return path


def append_verified_candidate(candidate: dict) -> Path:
    candidates = read_verified_candidates()
    candidate_id = str(candidate.get("candidate_id", ""))
    final_verified = bool(candidate.get("final_verified"))
    candidates = [
        item
        for item in candidates
        if not (
            str(item.get("candidate_id", "")) == candidate_id
            and bool(item.get("final_verified")) == final_verified
        )
    ]
    candidates.append(dict(candidate))
    return safe_json_write(
        verified_candidates_path(),
        {"format_version": 1, "candidates": candidates},
    )


def read_verified_candidates() -> list[dict]:
    path = verified_candidates_path()
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []
    candidates = payload.get("candidates", [])
    if not isinstance(candidates, list):
        return []
    return [dict(item) for item in candidates if isinstance(item, dict)]


def verified_candidate_payload(
    *,
    plan: list[dict],
    lock_clock_mhz: int,
    voltage_mv: int,
    probe: AutoUvProbeSummary | None,
    reason: str,
    label: str,
    base_probe: AutoUvProbeSummary | None = None,
    final_verified: bool = False,
    tail_rise_bins: int = 0,
) -> dict:
    flatten_target = build_flatten_target_for_plan(
        plan,
        plan,
        lock_clock_mhz=int(lock_clock_mhz),
        lock_voltage_mv=int(voltage_mv),
        tail_rise_bins=int(tail_rise_bins),
    )
    flatten_target["source"] = "auto-uv-final" if bool(final_verified) else "auto-uv"
    return {
        "candidate_id": candidate_id(
            voltage_mv=int(voltage_mv),
            lock_clock_mhz=int(lock_clock_mhz),
        ),
        "reason": str(reason),
        "label": str(label),
        "verified_at": datetime.now().astimezone().isoformat(),
        "final_verified": bool(final_verified),
        "lock_clock_mhz": int(lock_clock_mhz),
        "candidate_voltage_mv": int(voltage_mv),
        "tail_rise_bins": int(tail_rise_bins),
        "core_oc_mhz": core_oc_mhz(
            lock_clock_mhz=int(lock_clock_mhz),
            base_probe=base_probe,
        ),
        **probe_metrics(probe),
        **base_probe_metrics(base_probe),
        "flatten_target": flatten_target,
        "points": artifact_points(plan),
        "plan": list(plan),
    }


def candidate_id(*, voltage_mv: int, lock_clock_mhz: int) -> str:
    return f"{int(voltage_mv)}mv-{int(lock_clock_mhz)}mhz"


def probe_metrics(probe: AutoUvProbeSummary | None) -> dict:
    if probe is None:
        return {
            "avg_fps": None,
            "avg_core_clock_mhz": None,
            "loaded_median_core_clock_mhz": None,
            "loaded_p90_core_clock_mhz": None,
            "loaded_median_voltage_mv": None,
            "loaded_qualified_sample_count": 0,
            "observed_vdroop_mv": None,
            "avg_power_w": None,
            "max_power_w": None,
            "avg_temperature_c": None,
            "max_temperature_c": None,
            "avg_fan_speed_pct": None,
            "max_fan_speed_pct": None,
            "efficiency_fps_per_w": None,
            "efficiency_mhz_per_w": None,
            "watts_per_mhz": None,
            "perf_cap_reason": None,
            "fps_stddev": None,
            "fps_variance_pct": None,
        }
    return {
        "avg_fps": float_or_none(probe.avg_fps),
        "fps_stddev": float_or_none(probe.fps_stddev),
        "fps_variance_pct": float_or_none(probe.fps_variance_pct),
        "avg_core_clock_mhz": float_or_none(probe.avg_core_clock_mhz),
        "loaded_median_core_clock_mhz": float_or_none(
            probe.loaded_median_core_clock_mhz
        ),
        "loaded_p90_core_clock_mhz": float_or_none(probe.loaded_p90_core_clock_mhz),
        "loaded_median_voltage_mv": float_or_none(probe.loaded_median_voltage_mv),
        "loaded_qualified_sample_count": int_or_zero(
            probe.loaded_qualified_sample_count
        ),
        "observed_vdroop_mv": float_or_none(probe.observed_vdroop_mv),
        "avg_power_w": float_or_none(probe.avg_power_w),
        "max_power_w": float_or_none(probe.max_power_w),
        "avg_temperature_c": float_or_none(probe.avg_temperature_c),
        "max_temperature_c": float_or_none(probe.max_temperature_c),
        "avg_fan_speed_pct": float_or_none(probe.avg_fan_speed_pct),
        "max_fan_speed_pct": float_or_none(probe.max_fan_speed_pct),
        "efficiency_fps_per_w": float_or_none(probe.efficiency_fps_per_w),
        "efficiency_mhz_per_w": float_or_none(probe.efficiency_mhz_per_w),
        "watts_per_mhz": float_or_none(probe.watts_per_mhz),
        "perf_cap_reason": str_or_none(probe.perf_cap_reason),
    }


def base_probe_metrics(probe: AutoUvProbeSummary | None) -> dict:
    if probe is None:
        return {}
    return {
        f"base_{key}": value for key, value in probe_metrics(probe).items()
    } | {
        "base_candidate_voltage_mv": int(probe.candidate_voltage_mv),
        "base_lock_clock_mhz": int(probe.lock_clock_mhz),
        "base_avg_voltage_mv": float_or_none(probe.avg_voltage_mv),
    }


def core_oc_mhz(
    *,
    lock_clock_mhz: int,
    base_probe: AutoUvProbeSummary | None,
) -> int | None:
    if base_probe is None or base_probe.avg_core_clock_mhz is None:
        return None
    return int(round(float(lock_clock_mhz) - float(base_probe.avg_core_clock_mhz)))


def artifact_points(plan: list[dict]) -> list[dict]:
    return [
        {
            "index": int(point.index),
            "voltage_mv": int(point.voltage_mv),
            "base_mhz": int(point.base_mhz),
            "target_mhz": int(point.target_mhz),
            "new_offset_mhz": int(point.new_offset_mhz),
        }
        for point in read_base_vf_points(plan)
    ]


def float_or_none(value: object) -> float | None:
    return None if value is None else float(value)


def int_or_zero(value: object) -> int:
    return 0 if value is None else int(value)


def str_or_none(value: object) -> str | None:
    if value in (None, ""):
        return None
    return str(value)
