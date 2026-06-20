from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
import time

from common.penguin_burner_paths import claim_desktop_user_ownership, default_user_config_dir

from .profile_tiers import (
    load_profile_tier_assignments,
    load_profile_tier_disabled_profile_ids,
    profile_tier_summary_fields,
)


_PROFILE_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_USER_EDITED_PROFILE_SOURCE = "user-edited"


def auto_uv_profiles_dir() -> Path:
    return default_user_config_dir() / "auto-uv-profiles"


def _now() -> datetime:
    return datetime.now().astimezone()


def _now_iso() -> str:
    return _now().isoformat()


def _file_time_iso(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat()
    except OSError:
        return _now_iso()


def _safe_json_write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    claim_desktop_user_ownership(path.parent, include_parents=True)
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(path)
    claim_desktop_user_ownership(path)
    return path


def _read_json(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    return dict(payload) if isinstance(payload, dict) else None


def _safe_profile_id(value: str) -> str:
    text = _PROFILE_ID_SAFE_RE.sub("-", str(value).strip()).strip("-")
    return text or "profile"


def _candidate_id(payload: dict) -> str:
    candidate_id = str(payload.get("candidate_id", "")).strip()
    if candidate_id:
        return _safe_profile_id(candidate_id)
    voltage = payload.get("candidate_voltage_mv", payload.get("voltage_mv", "na"))
    clock = payload.get("lock_clock_mhz", payload.get("clock_mhz", "na"))
    return _safe_profile_id(f"{voltage}mv-{clock}mhz")


def _profile_sort_time(profile: dict) -> float:
    for key in ("profile_created_at", "verified_at", "created_at"):
        value = str(profile.get(key, "")).strip()
        if not value:
            continue
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            continue
    path = Path(str(profile.get("path", ""))).expanduser()
    try:
        return float(path.stat().st_mtime)
    except OSError:
        return 0.0


def archive_auto_uv_profile(final_curve_payload: dict) -> Path:
    timestamp = _now()
    created_at = timestamp.isoformat()
    candidate_id = _candidate_id(final_curve_payload)
    stamp = timestamp.strftime("%Y%m%d-%H%M%S-%f")
    profile_id = _safe_profile_id(
        str(final_curve_payload.get("profile_id", "")).strip()
        or f"{stamp}-{candidate_id}"
    )
    payload = dict(final_curve_payload)
    payload.update(
        {
            "format_version": 1,
            "profile_id": profile_id,
            "profile_created_at": created_at,
            "profile_source": str(payload.get("profile_source") or "auto-uv-final"),
        }
    )
    return _safe_json_write(
        auto_uv_profiles_dir() / f"auto-uv-profile-{profile_id}.json",
        payload,
    )


def mark_auto_uv_profile_verified(
    selector: str | Path,
    *,
    verification: dict | None = None,
    metrics: dict | None = None,
    base_metrics: dict | None = None,
) -> Path:
    resolved = resolve_auto_uv_profile(str(selector), allow_unverified=True)
    if resolved is None:
        raise FileNotFoundError(f"Auto-UV profile not found: {selector}")
    path, _resolved_payload = resolved
    payload = _read_json(path)
    if payload is None:
        raise FileNotFoundError(f"Auto-UV profile not readable: {path}")

    now = _now_iso()
    updated = dict(payload)
    updated["final_verified"] = True
    updated["requires_verification"] = False
    updated["verification_status"] = "verified"
    updated["verified_at"] = now
    updated.setdefault("profile_source", "auto-uv-final")

    merged_verification = {}
    if isinstance(updated.get("verification"), dict):
        merged_verification.update(updated["verification"])
    merged_verification["verified_at"] = now
    if isinstance(verification, dict):
        merged_verification.update(
            {
                key: value
                for key, value in verification.items()
                if value not in (None, "")
            }
        )
    updated["verification"] = merged_verification

    if isinstance(metrics, dict):
        for key in _VERIFICATION_METRIC_KEYS:
            value = metrics.get(key)
            if value in (None, "") or updated.get(key) not in (None, ""):
                continue
            updated[key] = value

    if isinstance(base_metrics, dict):
        for source_key, target_key in _BASE_VERIFICATION_METRIC_KEYS.items():
            value = base_metrics.get(source_key)
            if value in (None, "") or updated.get(target_key) not in (None, ""):
                continue
            updated[target_key] = value

    return _safe_json_write(path, updated)


def mark_auto_uv_profile_verification_failed(
    selector: str | Path,
    *,
    failure: dict | None = None,
) -> Path | None:
    resolved = resolve_auto_uv_profile(str(selector), allow_unverified=True)
    if resolved is None:
        raise FileNotFoundError(f"Auto-UV profile not found: {selector}")
    path, _resolved_payload = resolved
    payload = _read_json(path)
    if payload is None:
        raise FileNotFoundError(f"Auto-UV profile not readable: {path}")
    if not _is_user_edited_profile_payload(payload):
        return None

    now = _now_iso()
    updated = dict(payload)
    updated["final_verified"] = False
    updated["requires_verification"] = True
    updated["verification_status"] = "failed"
    updated["verification_failed_at"] = now

    merged_verification = {}
    if isinstance(updated.get("verification"), dict):
        merged_verification.update(updated["verification"])
    merged_verification["failed_at"] = now
    if isinstance(failure, dict):
        merged_verification["failure"] = {
            key: value
            for key, value in failure.items()
            if value not in (None, "")
        }
    updated["verification"] = merged_verification
    return _safe_json_write(path, updated)


_VERIFICATION_METRIC_KEYS = (
    "avg_core_clock_mhz",
    "avg_fps",
    "avg_power_w",
    "max_power_w",
    "avg_voltage_mv",
    "max_voltage_mv",
    "avg_temperature_c",
    "max_temperature_c",
    "avg_fan_speed_pct",
    "max_fan_speed_pct",
    "efficiency_fps_per_w",
    "efficiency_mhz_per_w",
    "watts_per_mhz",
)

_BASE_VERIFICATION_METRIC_KEYS = {
    "avg_core_clock_mhz": "base_avg_core_clock_mhz",
    "avg_fps": "base_avg_fps",
    "avg_power_w": "base_avg_power_w",
    "efficiency_fps_per_w": "base_efficiency_fps_per_w",
    "avg_voltage_mv": "base_avg_voltage_mv",
    "max_power_w": "base_max_power_w",
    "max_voltage_mv": "base_max_voltage_mv",
    "avg_temperature_c": "base_avg_temperature_c",
    "max_temperature_c": "base_max_temperature_c",
    "avg_fan_speed_pct": "base_avg_fan_speed_pct",
    "max_fan_speed_pct": "base_max_fan_speed_pct",
}


def _normalize_profile_payload(
    payload: dict,
    *,
    path: Path,
    source: str,
    include_unverified_user_edits: bool = True,
) -> dict | None:
    points = payload.get("points")
    plan = payload.get("plan")
    if not isinstance(points, list) and not isinstance(plan, list):
        return None

    candidate_voltage_mv = payload.get("candidate_voltage_mv", payload.get("voltage_mv"))
    lock_clock_mhz = payload.get("lock_clock_mhz", payload.get("clock_mhz"))
    if candidate_voltage_mv in (None, "") or lock_clock_mhz in (None, ""):
        return None
    if not _profile_payload_is_visible(
        payload,
        include_unverified_user_edits=include_unverified_user_edits,
    ):
        return None

    profile = dict(payload)
    profile.setdefault("profile_source", source)
    profile["path"] = str(path)
    profile.setdefault("candidate_id", _candidate_id(profile))
    profile.setdefault("profile_id", _safe_profile_id(str(profile["candidate_id"])))
    profile.setdefault("profile_created_at", _file_time_iso(path))
    return profile


def _is_final_verified_profile_payload(payload: dict) -> bool:
    return bool(payload.get("final_verified", False))


def _is_user_edited_profile_payload(payload: dict) -> bool:
    return str(payload.get("profile_source", "")).strip() == _USER_EDITED_PROFILE_SOURCE


def _profile_payload_is_visible(
    payload: dict,
    *,
    include_unverified_user_edits: bool,
) -> bool:
    if _is_final_verified_profile_payload(payload):
        return True
    return bool(
        include_unverified_user_edits
        and _is_user_edited_profile_payload(payload)
        and payload.get("requires_verification", True)
    )


def _profile_payload_is_runnable(
    payload: dict,
    *,
    allow_unverified: bool = False,
) -> bool:
    if _is_final_verified_profile_payload(payload):
        return True
    return bool(allow_unverified and _is_user_edited_profile_payload(payload))


def _load_profile_files(
    paths: list[Path],
    *,
    source: str,
    include_unverified_user_edits: bool = True,
) -> list[dict]:
    profiles = []
    for path in paths:
        payload = _read_json(path)
        if payload is None:
            continue
        profile = _normalize_profile_payload(
            payload,
            path=path,
            source=source,
            include_unverified_user_edits=include_unverified_user_edits,
        )
        if profile is not None:
            profiles.append(profile)
    return profiles


def read_auto_uv_profiles(
    *,
    include_unverified_user_edits: bool = True,
) -> list[dict]:
    profile_paths = sorted(auto_uv_profiles_dir().glob("*.json"))
    profiles = _load_profile_files(
        profile_paths,
        source="profile-store",
        include_unverified_user_edits=include_unverified_user_edits,
    )
    profiles.sort(key=_profile_sort_time, reverse=True)
    return profiles


def profile_summary(
    profile: dict,
    *,
    tier_assignments: dict[str, str] | None = None,
    disabled_profile_tier_ids: set[str] | None = None,
) -> dict:
    summary = {
        "profile_id": str(profile.get("profile_id", "")),
        "candidate_id": str(profile.get("candidate_id", "")),
        "display_name": str(profile.get("display_name", "")),
        "profile_created_at": str(profile.get("profile_created_at", "")),
        "verified_at": profile.get("verified_at"),
        "profile_source": str(profile.get("profile_source", "")),
        "path": str(profile.get("path", "")),
        "candidate_voltage_mv": profile.get("candidate_voltage_mv"),
        "lock_clock_mhz": profile.get("lock_clock_mhz"),
        "memory_offset_mhz": profile.get("memory_offset_mhz"),
        "power_limit_w": profile.get("power_limit_w"),
        "avg_core_clock_mhz": profile.get("avg_core_clock_mhz"),
        "avg_fps": profile.get("avg_fps"),
        "avg_power_w": profile.get("avg_power_w"),
        "efficiency_fps_per_w": profile.get("efficiency_fps_per_w"),
        "base_candidate_voltage_mv": profile.get("base_candidate_voltage_mv"),
        "base_lock_clock_mhz": profile.get("base_lock_clock_mhz"),
        "base_avg_core_clock_mhz": profile.get("base_avg_core_clock_mhz"),
        "base_avg_fps": profile.get("base_avg_fps"),
        "base_avg_power_w": profile.get("base_avg_power_w"),
        "base_efficiency_fps_per_w": profile.get("base_efficiency_fps_per_w"),
        "final_verified": bool(profile.get("final_verified", False)),
        "requires_verification": bool(profile.get("requires_verification", False)),
        "verification_status": profile.get("verification_status"),
        "manual_edit": profile.get("manual_edit"),
    }
    summary.update(
        profile_tier_summary_fields(
            profile,
            tier_assignments,
            disabled_profile_ids=disabled_profile_tier_ids,
        )
    )
    return summary


def profile_display_name(profile: dict) -> str:
    clock = _display_number(profile.get("lock_clock_mhz"), precision=0)
    voltage = _display_number(profile.get("candidate_voltage_mv"), precision=0)
    if clock and voltage:
        return f"{clock} MHz {voltage} mV"
    if clock:
        return f"{clock} MHz"
    if voltage:
        return f"{voltage} mV"
    return str(profile.get("candidate_id") or profile.get("profile_id") or "")


def _display_date(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return text[:19].replace("T", " ")


def read_auto_uv_profile_summaries() -> list[dict]:
    tier_assignments = load_profile_tier_assignments()
    disabled_profile_tier_ids = load_profile_tier_disabled_profile_ids()
    return [
        profile_summary(
            profile,
            tier_assignments=tier_assignments,
            disabled_profile_tier_ids=disabled_profile_tier_ids,
        )
        for profile in read_auto_uv_profiles()
    ]


def delete_auto_uv_profiles(selectors: list[str | Path]) -> list[Path]:
    paths: list[Path] = []
    for selector in selectors:
        text = str(selector or "").strip()
        if not text:
            continue
        resolved = resolve_auto_uv_profile(text, allow_unverified=True)
        if resolved is not None:
            paths.append(resolved[0])
        else:
            paths.append(Path(text).expanduser())
    return delete_auto_uv_profile_paths(paths)


def delete_auto_uv_profile_paths(paths: list[str | Path]) -> list[Path]:
    deleted: list[Path] = []
    seen: set[Path] = set()
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        try:
            resolved = path.resolve(strict=False)
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if not _is_deletable_auto_uv_profile_path(resolved):
            continue
        claim_desktop_user_ownership(path.parent, include_parents=True)
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        deleted.append(resolved)
    return deleted


def resolve_auto_uv_profile(
    selector: str,
    *,
    allow_unverified: bool = False,
) -> tuple[Path, dict] | None:
    text = str(selector or "").strip()
    if not text:
        return None

    if text in {"active", "latest"}:
        profiles = read_auto_uv_profiles(
            include_unverified_user_edits=bool(allow_unverified)
        )
        if not profiles:
            return None
        profile = profiles[0]
        path = Path(str(profile.get("path", ""))).expanduser()
        return path, profile

    path = Path(text).expanduser()
    if path.is_file():
        payload = _read_json(path)
        if payload is None:
            return None
        if not _profile_payload_is_runnable(
            payload,
            allow_unverified=bool(allow_unverified),
        ):
            return None
        return path, payload

    for profile in read_auto_uv_profiles(
        include_unverified_user_edits=bool(allow_unverified)
    ):
        if text in {
            str(profile.get("profile_id", "")),
            str(profile.get("candidate_id", "")),
            Path(str(profile.get("path", ""))).name,
            Path(str(profile.get("path", ""))).stem,
        }:
            path = Path(str(profile.get("path", ""))).expanduser()
            return path, profile
    return None


def _is_deletable_auto_uv_profile_path(path: Path) -> bool:
    profiles_dir = auto_uv_profiles_dir().resolve()
    resolved = path.resolve(strict=False)
    if resolved.suffix.lower() != ".json":
        return False
    try:
        resolved.relative_to(profiles_dir)
    except ValueError:
        return False
    return True


def format_profile_table(profiles: list[dict]) -> str:
    rows = [
        (
            "created",
            "profile",
            "mV",
            "target MHz",
            "effective MHz",
            "FPS/W",
            "Mem",
            "source",
        )
    ]
    for profile in profiles:
        created = _display_date(
            profile.get("profile_created_at") or profile.get("verified_at")
        )
        rows.append(
            (
                created,
                profile_display_name(profile),
                _display_number(profile.get("candidate_voltage_mv"), precision=0),
                _display_number(profile.get("lock_clock_mhz"), precision=0),
                _display_number(profile.get("avg_core_clock_mhz"), precision=2),
                _display_number(profile.get("efficiency_fps_per_w"), precision=4),
                _display_signed_number(profile.get("memory_offset_mhz"), precision=0),
                str(profile.get("profile_source", "")),
            )
        )
    widths = [max(len(str(row[index])) for row in rows) for index in range(len(rows[0]))]
    lines = []
    for row in rows:
        lines.append(
            "  ".join(str(value).ljust(widths[index]) for index, value in enumerate(row))
        )
    return "\n".join(lines)


def _display_number(value, *, precision: int) -> str:
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if precision <= 0:
        return str(int(round(number)))
    return f"{number:.{int(precision)}f}"


def _display_signed_number(value, *, precision: int) -> str:
    text = _display_number(value, precision=precision)
    if not text:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return text
    if abs(number) < 0.5:
        return "0"
    return f"+{text}" if number > 0 else text


def wait_for_new_profile(after_timestamp: float, *, timeout_s: float = 3.0) -> dict | None:
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while time.monotonic() <= deadline:
        profiles = read_auto_uv_profiles()
        for profile in profiles:
            if _profile_sort_time(profile) >= float(after_timestamp):
                return profile
        time.sleep(0.1)
    return None
