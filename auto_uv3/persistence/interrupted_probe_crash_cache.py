"""Turn a stale in-progress probe marker into an unsafe-voltage blacklist entry.

The marker is trusted only for aggressive undervolt recovery crashes, not normal Ctrl-C or clock-floor exits.
"""

from __future__ import annotations

import json
from pathlib import Path

from .auto_uv_persisted_json_files import probe_in_progress_path
from .unsafe_voltage_blacklist_file import record_unsafe_voltage


CRASH_CACHE_MIN_CLOCK_RECOVERY_PCT = 110.0
CRASH_CACHE_MIN_VOLTAGE_DROP_PCT = 5.0


def consume_interrupted_probe_crash_marker() -> tuple[Path, dict] | None:
    path = probe_in_progress_path()
    if not path.is_file():
        return None
    try:
        marker = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        marker = {}
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    if not isinstance(marker, dict) or marker.get("state") != "probing":
        return None
    try:
        candidate_voltage_mv = int(marker["candidate_voltage_mv"])
        lock_clock_mhz = int(marker["lock_clock_mhz"])
    except (KeyError, TypeError, ValueError):
        return None

    validation = interrupted_marker_crash_cache_validation(marker)
    if not bool(validation.get("accepted")):
        return None
    details = marker_details(marker)
    return record_unsafe_voltage(
        candidate_voltage_mv=candidate_voltage_mv,
        lock_clock_mhz=lock_clock_mhz,
        reason="previous-run-abruptly-ended",
        phase=str(marker.get("phase") or ""),
        marker_started_at=str(marker.get("started_at") or ""),
        blocked_lock_clock_mhz=details.get("blocked_lock_clock_mhz"),
        details={
            "marker_pid": marker.get("pid"),
            "marker_host": marker.get("host"),
            "marker_log_context": marker.get("log_context"),
            "marker_details": details,
            "crash_cache_validation": validation,
            "classification": (
                "stale probing marker remained on disk; clean Ctrl-C/SIGTERM "
                "cleanup removes this marker"
            ),
        },
    )


def interrupted_marker_crash_cache_validation(marker: dict) -> dict:
    candidate_voltage_mv = marker_float(marker, "candidate_voltage_mv")
    start_voltage_mv = marker_float(marker, "start_voltage_mv")
    voltage_drop_pct = marker_float(marker, "voltage_drop_from_start_pct")
    if (
        voltage_drop_pct is None
        and candidate_voltage_mv is not None
        and start_voltage_mv is not None
        and float(start_voltage_mv) > 0.0
    ):
        voltage_drop_pct = (
            (float(start_voltage_mv) - float(candidate_voltage_mv))
            / float(start_voltage_mv)
            * 100.0
        )

    clock_recovery_pct = marker_float(marker, "clock_recovery_pct")
    if clock_recovery_pct is None:
        clock_recovery_pct = marker_float(
            marker,
            "overclock_budget_used_of_clock_drop_pct",
        )
    if clock_recovery_pct is None:
        used_pct = marker_float(marker, "overclock_budget_used_pct")
        clock_drop_pct = marker_float(marker, "overclock_budget_clock_drop_pct")
        if used_pct is not None and clock_drop_pct not in (None, 0.0):
            clock_recovery_pct = float(used_pct) / float(clock_drop_pct) * 100.0

    validation = {
        "voltage_drop_from_start_pct": (
            round(float(voltage_drop_pct), 4)
            if voltage_drop_pct is not None
            else None
        ),
        "clock_recovery_pct": (
            round(float(clock_recovery_pct), 4)
            if clock_recovery_pct is not None
            else None
        ),
        "min_voltage_drop_pct": float(CRASH_CACHE_MIN_VOLTAGE_DROP_PCT),
        "min_clock_recovery_pct": float(CRASH_CACHE_MIN_CLOCK_RECOVERY_PCT),
    }
    if voltage_drop_pct is None or clock_recovery_pct is None:
        validation["accepted"] = False
        validation["reason"] = "missing crash-cache validation metrics"
        return validation
    voltage_ok = float(voltage_drop_pct) >= float(CRASH_CACHE_MIN_VOLTAGE_DROP_PCT)
    recovery_ok = float(clock_recovery_pct) >= float(
        CRASH_CACHE_MIN_CLOCK_RECOVERY_PCT
    )
    validation["accepted"] = bool(voltage_ok and recovery_ok)
    if not voltage_ok:
        validation["reason"] = "voltage drop below crash-cache threshold"
    elif not recovery_ok:
        validation["reason"] = "clock recovery below crash-cache threshold"
    else:
        validation["reason"] = "validated aggressive undervolt crash marker"
    return validation


def marker_details(marker: dict) -> dict:
    details = marker.get("details")
    return details if isinstance(details, dict) else {}


def marker_float(marker: dict, key: str) -> float | None:
    for source in (marker, marker_details(marker)):
        try:
            value = source.get(key)
        except AttributeError:
            continue
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None
