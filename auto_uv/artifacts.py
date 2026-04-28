from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import socket

from penguin_burner_paths import claim_desktop_user_ownership

from .artifact_paths import auto_uv_saved_uv_dir, auto_uv_user_config_dir
from .models import AutoUvProbeSummary, VoltageCurve
from .profiles import archive_auto_uv_profile
from .unsafe_classification import _unsafe_entry_blocks_future_search


CRASH_CACHE_MIN_CLOCK_RECOVERY_PCT = 110.0
CRASH_CACHE_MIN_VOLTAGE_DROP_PCT = 5.0


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _safe_json_write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    claim_desktop_user_ownership(path.parent, include_parents=True)
    temp_path = path.with_name(path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temp_path.replace(path)
    try:
        directory_fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        directory_fd = None
    if directory_fd is not None:
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    claim_desktop_user_ownership(path)
    return path


def _probe_in_progress_path() -> Path:
    return auto_uv_user_config_dir() / "uv-result" / "auto-uv-probe-in-progress.json"


def _unsafe_voltage_blacklist_path() -> Path:
    return auto_uv_user_config_dir() / "uv-result" / "auto-uv-unsafe-voltages.json"


def _verified_candidates_path() -> Path:
    return auto_uv_user_config_dir() / "uv-result" / "auto-uv-verified-candidates.json"


def _final_choice_request_path() -> Path:
    return auto_uv_user_config_dir() / "auto-uv-final-choice-request.json"


def _final_choice_response_path() -> Path:
    return auto_uv_user_config_dir() / "auto-uv-final-choice.json"


def _probe_artifact_metrics(probe: AutoUvProbeSummary | None) -> dict:
    if probe is None:
        return {
            "avg_fps": None,
            "avg_core_clock_mhz": None,
            "avg_power_w": None,
            "max_power_w": None,
            "avg_temperature_c": None,
            "max_temperature_c": None,
            "avg_fan_speed_pct": None,
            "max_fan_speed_pct": None,
            "efficiency_fps_per_w": None,
            "efficiency_mhz_per_w": None,
            "watts_per_mhz": None,
        }
    return {
        "avg_fps": float(probe.avg_fps) if probe.avg_fps is not None else None,
        "avg_core_clock_mhz": (
            float(probe.avg_core_clock_mhz)
            if probe.avg_core_clock_mhz is not None
            else None
        ),
        "avg_power_w": (
            float(probe.avg_power_w) if probe.avg_power_w is not None else None
        ),
        "max_power_w": (
            float(probe.max_power_w) if probe.max_power_w is not None else None
        ),
        "avg_temperature_c": (
            float(probe.avg_temperature_c)
            if probe.avg_temperature_c is not None
            else None
        ),
        "max_temperature_c": (
            float(probe.max_temperature_c)
            if probe.max_temperature_c is not None
            else None
        ),
        "avg_fan_speed_pct": (
            float(probe.avg_fan_speed_pct)
            if probe.avg_fan_speed_pct is not None
            else None
        ),
        "max_fan_speed_pct": (
            float(probe.max_fan_speed_pct)
            if probe.max_fan_speed_pct is not None
            else None
        ),
        "efficiency_fps_per_w": (
            float(probe.efficiency_fps_per_w)
            if probe.efficiency_fps_per_w is not None
            else None
        ),
        "efficiency_mhz_per_w": (
            float(probe.efficiency_mhz_per_w)
            if probe.efficiency_mhz_per_w is not None
            else None
        ),
        "watts_per_mhz": (
            float(probe.watts_per_mhz) if probe.watts_per_mhz is not None else None
        ),
    }


def _base_probe_artifact_metrics(probe: AutoUvProbeSummary | None) -> dict:
    if probe is None:
        return {}
    payload = {
        f"base_{key}": value
        for key, value in _probe_artifact_metrics(probe).items()
    }
    payload.update(
        {
            "base_candidate_voltage_mv": int(probe.candidate_voltage_mv),
            "base_lock_clock_mhz": int(probe.lock_clock_mhz),
            "base_avg_voltage_mv": (
                float(probe.avg_voltage_mv)
                if probe.avg_voltage_mv is not None
                else None
            ),
        }
    )
    return payload


def _candidate_id(*, voltage_mv: int, lock_clock_mhz: int) -> str:
    return f"{int(voltage_mv)}mv-{int(lock_clock_mhz)}mhz"


def _uv_candidate_payload(
    *,
    plan: list[dict],
    lock_clock_mhz: int,
    voltage_mv: int,
    probe: AutoUvProbeSummary | None,
    reason: str,
    label: str | None = None,
    final_verified: bool = False,
    base_probe: AutoUvProbeSummary | None = None,
) -> dict:
    payload = {
        "candidate_id": _candidate_id(
            voltage_mv=int(voltage_mv),
            lock_clock_mhz=int(lock_clock_mhz),
        ),
        "reason": str(reason),
        "label": str(label) if label is not None else str(reason),
        "verified_at": _now_iso(),
        "final_verified": bool(final_verified),
        "lock_clock_mhz": int(lock_clock_mhz),
        "candidate_voltage_mv": int(voltage_mv),
        **_probe_artifact_metrics(probe),
        **_base_probe_artifact_metrics(base_probe),
        "points": VoltageCurve.from_plan(plan).artifact_points(),
        "plan": list(plan),
    }
    return payload


def _read_verified_uv_candidates() -> list[dict]:
    path = _verified_candidates_path()
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


def _append_verified_uv_candidate(candidate: dict) -> Path:
    candidates = _read_verified_uv_candidates()
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
    return _safe_json_write(
        _verified_candidates_path(),
        {"format_version": 1, "candidates": candidates},
    )


def _write_final_curve_snapshot(
    *,
    plan: list[dict],
    lock_clock_mhz: int,
    voltage_mv: int,
    probe: AutoUvProbeSummary | None,
    base_probe: AutoUvProbeSummary | None = None,
    fan_curve_payload: dict | None = None,
    memory_offset_mhz: int | None = None,
) -> Path:
    payload = _uv_candidate_payload(
        plan=plan,
        lock_clock_mhz=int(lock_clock_mhz),
        voltage_mv=int(voltage_mv),
        probe=probe,
        base_probe=base_probe,
        reason="final-verified",
        label="final-verified",
        final_verified=True,
    )
    if isinstance(fan_curve_payload, dict):
        payload["fan_curve_payload"] = dict(fan_curve_payload)
    if memory_offset_mhz is not None:
        payload["memory_offset_mhz"] = int(memory_offset_mhz)
    _append_verified_uv_candidate(payload)
    return archive_auto_uv_profile(payload)


def _write_uv_result_snapshot(
    *,
    plan: list[dict],
    lock_clock_mhz: int,
    voltage_mv: int,
    probe: AutoUvProbeSummary | None,
    reason: str,
) -> Path:
    output_dir = auto_uv_user_config_dir() / "uv-result"
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    output_path = output_dir / f"auto-uv-{reason}-{timestamp}.json"
    payload = _uv_candidate_payload(
        plan=plan,
        lock_clock_mhz=int(lock_clock_mhz),
        voltage_mv=int(voltage_mv),
        probe=probe,
        reason=str(reason),
        label=str(reason),
    )
    return _safe_json_write(output_path, payload)


def _write_saved_uv_state(
    *,
    plan: list[dict],
    lock_clock_mhz: int,
    voltage_mv: int,
    probe: AutoUvProbeSummary | None,
    label: str,
    verification_duration_s: int | None = None,
) -> Path:
    output_dir = auto_uv_saved_uv_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    safe_label = "".join(
        char if char.isalnum() or char in ("-", "_") else "-" for char in str(label)
    ).strip("-")
    if not safe_label:
        safe_label = "saved"
    output_path = output_dir / f"auto-uv-{safe_label}-{timestamp}.json"
    payload = {
        "label": str(label),
        "lock_clock_mhz": int(lock_clock_mhz),
        "candidate_voltage_mv": int(voltage_mv),
        "verification_duration_s": (
            int(verification_duration_s)
            if verification_duration_s is not None
            else None
        ),
        **_probe_artifact_metrics(probe),
        "points": VoltageCurve.from_plan(plan).artifact_points(),
    }
    return _safe_json_write(output_path, payload)


def _write_latest_verified_uv_result(
    *,
    plan: list[dict],
    lock_clock_mhz: int,
    voltage_mv: int,
    probe: AutoUvProbeSummary | None,
    base_probe: AutoUvProbeSummary | None = None,
) -> Path:
    output_dir = auto_uv_user_config_dir() / "uv-result"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "auto-uv-latest-verified.json"
    payload = _uv_candidate_payload(
        plan=plan,
        lock_clock_mhz=int(lock_clock_mhz),
        voltage_mv=int(voltage_mv),
        probe=probe,
        base_probe=base_probe,
        reason="latest-verified",
        label="passed-short-probe",
    )
    path = _safe_json_write(output_path, payload)
    _append_verified_uv_candidate(payload)
    return path


def _write_stable_uv_result(
    *,
    plan: list[dict],
    lock_clock_mhz: int,
    voltage_mv: int,
    probe: AutoUvProbeSummary | None,
    label: str,
    verification_duration_s: int | None = None,
) -> Path:
    output_dir = auto_uv_user_config_dir() / "uv-result"
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_label = "".join(
        char if char.isalnum() or char in ("-", "_") else "-" for char in str(label)
    ).strip("-")
    if not safe_label:
        safe_label = "stable"
    output_path = output_dir / f"auto-uv-{safe_label}-stable.json"
    payload = {
        "reason": "stable",
        "label": str(label),
        "stable": True,
        "verification_duration_s": (
            int(verification_duration_s)
            if verification_duration_s is not None
            else None
        ),
        "lock_clock_mhz": int(lock_clock_mhz),
        "candidate_voltage_mv": int(voltage_mv),
        **_probe_artifact_metrics(probe),
        "points": VoltageCurve.from_plan(plan).artifact_points(),
    }
    return _safe_json_write(output_path, payload)


def _write_uv_probe_in_progress(
    *,
    phase: str,
    candidate_voltage_mv: int,
    lock_clock_mhz: int,
    log_context: str | None = None,
    details: dict | None = None,
) -> Path:
    payload = {
        "format_version": 1,
        "state": "probing",
        "started_at": _now_iso(),
        "pid": int(os.getpid()),
        "host": socket.gethostname(),
        "phase": str(phase),
        "candidate_voltage_mv": int(candidate_voltage_mv),
        "lock_clock_mhz": int(lock_clock_mhz),
        "log_context": str(log_context).strip() if log_context is not None else None,
    }
    if details:
        payload["details"] = details
    return _safe_json_write(_probe_in_progress_path(), payload)


def _clear_uv_probe_in_progress() -> None:
    path = _probe_in_progress_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _load_uv_unsafe_voltage_entries() -> list[dict]:
    path = _unsafe_voltage_blacklist_path()
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return []
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []
    return [
        dict(entry)
        for entry in entries
        if isinstance(entry, dict) and _unsafe_entry_blocks_future_search(entry)
    ]


def _write_uv_unsafe_voltage_entries(entries: list[dict]) -> Path:
    payload = {
        "format_version": 1,
        "updated_at": _now_iso(),
        "entries": entries,
    }
    return _safe_json_write(_unsafe_voltage_blacklist_path(), payload)


def _record_unsafe_uv_voltage(
    *,
    candidate_voltage_mv: int,
    lock_clock_mhz: int,
    reason: str,
    phase: str | None = None,
    marker_started_at: str | None = None,
    details: dict | None = None,
    blocked_lock_clock_mhz: list[int] | None = None,
) -> tuple[Path, dict]:
    blocked_locks = _normalize_blocked_lock_clocks(
        int(lock_clock_mhz),
        blocked_lock_clock_mhz,
    )
    entry = {
        "recorded_at": _now_iso(),
        "reason": str(reason),
        "candidate_voltage_mv": int(candidate_voltage_mv),
        "lock_clock_mhz": int(lock_clock_mhz),
        "phase": str(phase).strip() if phase else None,
        "marker_started_at": str(marker_started_at).strip()
        if marker_started_at
        else None,
    }
    if blocked_locks != [int(lock_clock_mhz)]:
        entry["blocked_lock_clock_mhz"] = blocked_locks
    if details:
        entry["details"] = details
    entries = _load_uv_unsafe_voltage_entries()
    deduped = []
    for item in entries:
        try:
            is_duplicate = (
                int(item.get("candidate_voltage_mv", -1))
                == int(candidate_voltage_mv)
                and int(item.get("lock_clock_mhz", -1)) == int(lock_clock_mhz)
                and str(item.get("reason", "")) == str(reason)
                and _normalize_blocked_lock_clocks(
                    int(item.get("lock_clock_mhz", -1)),
                    item.get("blocked_lock_clock_mhz"),
                )
                == blocked_locks
            )
        except (TypeError, ValueError):
            is_duplicate = False
        if not is_duplicate:
            deduped.append(item)
    deduped.append(entry)
    return _write_uv_unsafe_voltage_entries(deduped), entry


def _normalize_blocked_lock_clocks(
    lock_clock_mhz: int,
    blocked_lock_clock_mhz: object,
) -> list[int]:
    values = {int(lock_clock_mhz)} if int(lock_clock_mhz) > 0 else set()
    if isinstance(blocked_lock_clock_mhz, (list, tuple, set)):
        for value in blocked_lock_clock_mhz:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                values.add(int(parsed))
    return sorted(values, reverse=True)


def _marker_details(marker: dict) -> dict:
    details = marker.get("details")
    return details if isinstance(details, dict) else {}


def _marker_float(marker: dict, key: str) -> float | None:
    for source in (marker, _marker_details(marker)):
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


def _interrupted_marker_crash_cache_validation(marker: dict) -> dict:
    candidate_voltage_mv = _marker_float(marker, "candidate_voltage_mv")
    start_voltage_mv = _marker_float(marker, "start_voltage_mv")
    voltage_drop_pct = _marker_float(marker, "voltage_drop_from_start_pct")
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

    clock_recovery_pct = _marker_float(marker, "clock_recovery_pct")
    if clock_recovery_pct is None:
        clock_recovery_pct = _marker_float(
            marker,
            "overclock_budget_used_of_clock_drop_pct",
        )
    if clock_recovery_pct is None:
        used_pct = _marker_float(marker, "overclock_budget_used_pct")
        clock_drop_pct = _marker_float(marker, "overclock_budget_clock_drop_pct")
        if (
            used_pct is not None
            and clock_drop_pct is not None
            and float(clock_drop_pct) > 0.0
        ):
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


def _consume_interrupted_uv_probe_marker() -> tuple[Path, dict] | None:
    path = _probe_in_progress_path()
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

    if not isinstance(marker, dict):
        marker = {}
    if marker.get("state") != "probing":
        return None
    try:
        candidate_voltage_mv = int(marker["candidate_voltage_mv"])
        lock_clock_mhz = int(marker["lock_clock_mhz"])
    except (KeyError, TypeError, ValueError):
        return None
    validation = _interrupted_marker_crash_cache_validation(marker)
    if not bool(validation.get("accepted")):
        return None
    marker_details = _marker_details(marker)
    return _record_unsafe_uv_voltage(
        candidate_voltage_mv=candidate_voltage_mv,
        lock_clock_mhz=lock_clock_mhz,
        reason="previous-run-abruptly-ended",
        phase=str(marker.get("phase") or ""),
        marker_started_at=str(marker.get("started_at") or ""),
        blocked_lock_clock_mhz=marker_details.get("blocked_lock_clock_mhz"),
        details={
            "marker_pid": marker.get("pid"),
            "marker_host": marker.get("host"),
            "marker_log_context": marker.get("log_context"),
            "marker_details": marker_details,
            "crash_cache_validation": validation,
            "classification": (
                "stale probing marker remained on disk; clean Ctrl-C/SIGTERM "
                "cleanup removes this marker"
            ),
        },
    )
