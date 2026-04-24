from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

from penguin_burner_paths import default_saved_uv_dir, default_user_config_dir

from .models import AutoUvProbeSummary, VoltageCurve


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _safe_json_write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(json.dumps(payload, indent=2) + "\n")
    temp_path.replace(path)
    return path


def _probe_in_progress_path() -> Path:
    return default_user_config_dir() / "uv-result" / "auto-uv-probe-in-progress.json"


def _unsafe_voltage_blacklist_path() -> Path:
    return default_user_config_dir() / "uv-result" / "auto-uv-unsafe-voltages.json"


def _probe_artifact_metrics(probe: AutoUvProbeSummary | None) -> dict:
    if probe is None:
        return {
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


def _write_final_curve_snapshot(
    *,
    plan: list[dict],
    lock_clock_mhz: int,
    voltage_mv: int,
    probe: AutoUvProbeSummary | None,
) -> Path:
    payload = {
        "lock_clock_mhz": int(lock_clock_mhz),
        "candidate_voltage_mv": int(voltage_mv),
        **_probe_artifact_metrics(probe),
        "points": VoltageCurve.from_plan(plan).artifact_points(),
    }
    output_dir = default_user_config_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "auto-uv-final-curve.json"
    return _safe_json_write(output_path, payload)


def _write_uv_result_snapshot(
    *,
    plan: list[dict],
    lock_clock_mhz: int,
    voltage_mv: int,
    probe: AutoUvProbeSummary | None,
    reason: str,
) -> Path:
    output_dir = default_user_config_dir() / "uv-result"
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    output_path = output_dir / f"auto-uv-{reason}-{timestamp}.json"
    payload = {
        "reason": str(reason),
        "lock_clock_mhz": int(lock_clock_mhz),
        "candidate_voltage_mv": int(voltage_mv),
        **_probe_artifact_metrics(probe),
        "points": VoltageCurve.from_plan(plan).artifact_points(),
    }
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
    output_dir = default_saved_uv_dir()
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
) -> Path:
    output_dir = default_user_config_dir() / "uv-result"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "auto-uv-latest-verified.json"
    payload = {
        "reason": "latest-verified",
        "lock_clock_mhz": int(lock_clock_mhz),
        "candidate_voltage_mv": int(voltage_mv),
        **_probe_artifact_metrics(probe),
        "points": VoltageCurve.from_plan(plan).artifact_points(),
    }
    return _safe_json_write(output_path, payload)


def _write_stable_uv_result(
    *,
    plan: list[dict],
    lock_clock_mhz: int,
    voltage_mv: int,
    probe: AutoUvProbeSummary | None,
    label: str,
    verification_duration_s: int | None = None,
) -> Path:
    output_dir = default_user_config_dir() / "uv-result"
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
) -> Path:
    payload = {
        "format_version": 1,
        "state": "probing",
        "started_at": _now_iso(),
        "phase": str(phase),
        "candidate_voltage_mv": int(candidate_voltage_mv),
        "lock_clock_mhz": int(lock_clock_mhz),
        "log_context": str(log_context).strip() if log_context is not None else None,
    }
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
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []
    return [dict(entry) for entry in entries if isinstance(entry, dict)]


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
) -> tuple[Path, dict]:
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
    if details:
        entry["details"] = details
    entries = _load_uv_unsafe_voltage_entries()
    deduped = [
        item
        for item in entries
        if not (
            int(item.get("candidate_voltage_mv", -1)) == int(candidate_voltage_mv)
            and int(item.get("lock_clock_mhz", -1)) == int(lock_clock_mhz)
            and str(item.get("reason", "")) == str(reason)
        )
    ]
    deduped.append(entry)
    return _write_uv_unsafe_voltage_entries(deduped), entry


def _consume_interrupted_uv_probe_marker() -> tuple[Path, dict] | None:
    path = _probe_in_progress_path()
    if not path.is_file():
        return None
    try:
        marker = json.loads(path.read_text())
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
    return _record_unsafe_uv_voltage(
        candidate_voltage_mv=candidate_voltage_mv,
        lock_clock_mhz=lock_clock_mhz,
        reason="previous-run-interrupted",
        phase=str(marker.get("phase") or ""),
        marker_started_at=str(marker.get("started_at") or ""),
    )
