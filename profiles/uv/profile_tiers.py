from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import time

from common.penguin_burner_paths import claim_desktop_user_ownership, default_user_config_dir


PROFILE_TIER_EFFICIENCY = "efficiency"
PROFILE_TIER_BALANCED = "balanced"
PROFILE_TIER_PERFORMANCE = "performance"
PROFILE_TIER_NONE = "none"
PROFILE_TIERS = (
    PROFILE_TIER_EFFICIENCY,
    PROFILE_TIER_BALANCED,
    PROFILE_TIER_PERFORMANCE,
)
PROFILE_TIER_LABELS = {
    PROFILE_TIER_EFFICIENCY: "Efficiency",
    PROFILE_TIER_BALANCED: "Balanced",
    PROFILE_TIER_PERFORMANCE: "Performance",
}
_BALANCED_TAIL_RISE_BINS = 4
_PERFORMANCE_TAIL_RISE_BINS = 6

_PROFILE_TIER_ALIASES = {
    "eff": PROFILE_TIER_EFFICIENCY,
    "efficiency": PROFILE_TIER_EFFICIENCY,
    "balanced": PROFILE_TIER_BALANCED,
    "balance": PROFILE_TIER_BALANCED,
    "bal": PROFILE_TIER_BALANCED,
    "normal": PROFILE_TIER_BALANCED,
    "perf": PROFILE_TIER_PERFORMANCE,
    "performance": PROFILE_TIER_PERFORMANCE,
    "aggressive": PROFILE_TIER_PERFORMANCE,
}
_PROFILE_TIER_NONE_ALIASES = {"none", "no", "off", "disable", "disabled", "unassigned"}


def normalize_profile_tier(value: object | None, *, default: str = "") -> str:
    text = str(value or "").strip().lower()
    if not text:
        return default
    normalized = _PROFILE_TIER_ALIASES.get(text, "")
    return normalized or default


def profile_tier_label(value: object | None) -> str:
    tier = normalize_profile_tier(value)
    return PROFILE_TIER_LABELS.get(tier, "")


def profile_tier_is_none(value: object | None) -> bool:
    text = str(value or "").strip().lower()
    return text in _PROFILE_TIER_NONE_ALIASES


def _tier_from_tail_rise_bins(tail_rise_bins: int) -> str:
    # Single source of truth for the tail-bins -> tier thresholds, shared by
    # both the runtime-options and saved-profile classifiers so they can never
    # drift apart (efficiency=flat, balanced>=4 bins, performance>=6 bins).
    if tail_rise_bins >= _PERFORMANCE_TAIL_RISE_BINS:
        return PROFILE_TIER_PERFORMANCE
    if tail_rise_bins >= _BALANCED_TAIL_RISE_BINS:
        return PROFILE_TIER_BALANCED
    return PROFILE_TIER_EFFICIENCY


def _tier_from_mode(mode: str) -> str:
    # Mode-only fallback when no tail-bins signal is present: an explicit
    # efficiency mode maps to efficiency, everything else defaults to balanced.
    if mode == PROFILE_TIER_EFFICIENCY:
        return PROFILE_TIER_EFFICIENCY
    return PROFILE_TIER_BALANCED


def generated_profile_tier(profile: dict | None) -> str:
    payload = profile if isinstance(profile, dict) else {}
    for key in (
        "generated_profile_tier",
        "profile_generated_tier",
        "auto_uv_requested_mode",
        "auto_uv_profile_tier",
        "profile_tier",
    ):
        tier = normalize_profile_tier(payload.get(key))
        if tier:
            return tier

    mode = normalize_profile_tier(payload.get("auto_uv_mode"))
    if mode == PROFILE_TIER_PERFORMANCE:
        return PROFILE_TIER_PERFORMANCE

    profile_curve = str(payload.get("profile_curve") or "").strip().lower()
    if profile_curve in {"performance-sweep", "auto-oc"}:
        return PROFILE_TIER_PERFORMANCE

    tail_rise_bins = _optional_int(payload.get("tail_rise_bins"))
    if tail_rise_bins is None:
        flatten_target = payload.get("flatten_target")
        if isinstance(flatten_target, dict):
            tail_rise_bins = _optional_int(flatten_target.get("tail_rise_bins"))
    if tail_rise_bins is not None:
        return _tier_from_tail_rise_bins(tail_rise_bins)

    return _tier_from_mode(mode)


def profile_tier_assignments_path() -> Path:
    return default_user_config_dir() / "auto-uv-profile-tier-assignments.json"


def load_profile_tier_assignments(path: str | Path | None = None) -> dict[str, str]:
    payload = _read_profile_tier_assignments(path)
    raw_tiers = payload.get("tiers") if isinstance(payload, dict) else None
    if not isinstance(raw_tiers, dict):
        return {}
    assignments: dict[str, str] = {}
    seen_profiles: set[str] = set()
    disabled_profile_ids = _disabled_profile_ids_from_payload(payload)
    for raw_tier, raw_profile_id in raw_tiers.items():
        tier = normalize_profile_tier(raw_tier)
        profile_id = str(raw_profile_id or "").strip()
        if (
            not tier
            or not profile_id
            or profile_id in seen_profiles
            or profile_id in disabled_profile_ids
        ):
            continue
        assignments[tier] = profile_id
        seen_profiles.add(profile_id)
    return assignments


def load_profile_tier_disabled_profile_ids(
    path: str | Path | None = None,
) -> set[str]:
    return _disabled_profile_ids_from_payload(_read_profile_tier_assignments(path))


def save_profile_tier_assignment(
    profile_id: str,
    tier: object | None,
    *,
    path: str | Path | None = None,
) -> dict[str, str]:
    selected_profile_id = str(profile_id or "").strip()
    selected_tier = normalize_profile_tier(tier)
    if not selected_profile_id:
        raise ValueError("profile_id is required")
    if profile_tier_is_none(tier):
        return save_profile_tier_none_assignment(selected_profile_id, path=path)
    if not selected_tier:
        raise ValueError("profile tier is required")

    disabled_profile_ids = load_profile_tier_disabled_profile_ids(path)
    disabled_profile_ids.discard(selected_profile_id)
    assignments = {
        key: value
        for key, value in load_profile_tier_assignments(path).items()
        if value != selected_profile_id
    }
    assignments[selected_tier] = selected_profile_id
    _write_profile_tier_assignments(
        assignments,
        disabled_profile_ids=disabled_profile_ids,
        path=path,
    )
    return assignments


def save_profile_tier_none_assignment(
    profile_id: str,
    *,
    path: str | Path | None = None,
) -> dict[str, str]:
    selected_profile_id = str(profile_id or "").strip()
    if not selected_profile_id:
        raise ValueError("profile_id is required")

    assignments = {
        key: value
        for key, value in load_profile_tier_assignments(path).items()
        if value != selected_profile_id
    }
    disabled_profile_ids = load_profile_tier_disabled_profile_ids(path)
    disabled_profile_ids.add(selected_profile_id)
    _write_profile_tier_assignments(
        assignments,
        disabled_profile_ids=disabled_profile_ids,
        path=path,
    )
    return assignments


def profile_tier_disabled(
    profile: dict,
    disabled_profile_ids: set[str] | None = None,
) -> bool:
    if _truthy(profile.get("profile_tier_disabled")):
        return True
    profile_id = str(profile.get("profile_id") or "").strip()
    if not profile_id:
        return False
    disabled = (
        disabled_profile_ids
        if disabled_profile_ids is not None
        else load_profile_tier_disabled_profile_ids()
    )
    return profile_id in disabled


def assigned_tier_for_profile(
    profile: dict,
    assignments: dict[str, str] | None = None,
) -> str:
    profile_id = str(profile.get("profile_id") or "").strip()
    if not profile_id:
        return ""
    tier_assignments = assignments if assignments is not None else load_profile_tier_assignments()
    for tier, assigned_profile_id in tier_assignments.items():
        if str(assigned_profile_id or "").strip() == profile_id:
            return normalize_profile_tier(tier)
    return ""


def profile_tier_summary_fields(
    profile: dict,
    assignments: dict[str, str] | None = None,
    disabled_profile_ids: set[str] | None = None,
) -> dict[str, object]:
    generated_tier = generated_profile_tier(profile)
    disabled = profile_tier_disabled(profile, disabled_profile_ids)
    assigned_tier = "" if disabled else assigned_tier_for_profile(profile, assignments)
    effective_tier = "" if disabled else assigned_tier or generated_tier
    return {
        "generated_profile_tier": profile_tier_label(generated_tier),
        "generated_profile_tier_key": generated_tier,
        "assigned_profile_tier": profile_tier_label(assigned_tier),
        "assigned_profile_tier_key": assigned_tier,
        "profile_tier": profile_tier_label(effective_tier),
        "profile_tier_key": effective_tier,
        "profile_tier_disabled": disabled,
    }


def resolve_profile_tier_profiles(
    profiles: list[dict],
    assignments: dict[str, str] | None = None,
    disabled_profile_ids: set[str] | None = None,
) -> dict[str, dict | None]:
    tier_assignments = assignments if assignments is not None else load_profile_tier_assignments()
    disabled = (
        disabled_profile_ids
        if disabled_profile_ids is not None
        else load_profile_tier_disabled_profile_ids()
    )
    visible_profiles = [
        profile
        for profile in profiles
        if bool(profile.get("final_verified", False))
        and not profile_tier_disabled(profile, disabled)
    ]
    visible_profiles.sort(key=_profile_sort_time, reverse=True)
    by_profile_id = {
        str(profile.get("profile_id") or ""): profile
        for profile in visible_profiles
        if str(profile.get("profile_id") or "").strip()
    }
    resolved: dict[str, dict | None] = {tier: None for tier in PROFILE_TIERS}
    assigned_profile_ids: set[str] = set()

    for tier in PROFILE_TIERS:
        assigned_profile_id = str(tier_assignments.get(tier) or "").strip()
        profile = by_profile_id.get(assigned_profile_id)
        if profile is None:
            continue
        resolved[tier] = profile
        assigned_profile_ids.add(assigned_profile_id)

    unassigned_profiles = []
    for profile in visible_profiles:
        profile_id = str(profile.get("profile_id") or "").strip()
        if not profile_id or profile_id in assigned_profile_ids:
            continue
        tier = generated_profile_tier(profile)
        if tier in resolved:
            unassigned_profiles.append(profile)
    for tier in PROFILE_TIERS:
        if resolved[tier] is not None:
            continue
        candidates = [
            profile
            for profile in unassigned_profiles
            if generated_profile_tier(profile) == tier
        ]
        if not candidates:
            continue
        resolved[tier] = max(
            candidates,
            key=lambda profile, tier=tier, candidates=candidates: (
                _profile_tier_sort_key(profile, tier, candidates)
            ),
        )
    return resolved


def available_adaptive_tiers(
    resolved_profiles: dict[str, dict | None],
) -> list[str]:
    tiers: list[str] = []
    seen_profile_ids: set[str] = set()
    for tier in PROFILE_TIERS:
        profile = resolved_profiles.get(tier)
        if not isinstance(profile, dict):
            continue
        profile_id = str(profile.get("profile_id") or "").strip()
        if not profile_id or profile_id in seen_profile_ids:
            continue
        tiers.append(tier)
        seen_profile_ids.add(profile_id)
    return tiers


def _write_profile_tier_assignments(
    assignments: dict[str, str],
    *,
    disabled_profile_ids: set[str] | None = None,
    path: str | Path | None = None,
) -> Path:
    assignment_path = Path(path).expanduser() if path is not None else profile_tier_assignments_path()
    disabled = sorted(
        str(profile_id).strip()
        for profile_id in (disabled_profile_ids or set())
        if str(profile_id).strip()
    )
    payload = {
        "format_version": 2,
        "updated_at": datetime.now().astimezone().isoformat(),
        "tiers": {
            tier: str(assignments[tier])
            for tier in PROFILE_TIERS
            if str(assignments.get(tier) or "").strip()
        },
    }
    if disabled:
        payload["disabled_profile_ids"] = disabled
    assignment_path.parent.mkdir(parents=True, exist_ok=True)
    claim_desktop_user_ownership(assignment_path.parent, include_parents=True)
    temp_path = assignment_path.with_name(assignment_path.name + ".tmp")
    temp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(assignment_path)
    claim_desktop_user_ownership(assignment_path)
    return assignment_path


def _read_profile_tier_assignments(path: str | Path | None = None) -> dict:
    assignment_path = Path(path).expanduser() if path is not None else profile_tier_assignments_path()
    try:
        payload = json.loads(
            assignment_path.read_text(encoding="utf-8", errors="replace")
        )
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _disabled_profile_ids_from_payload(payload: dict) -> set[str]:
    raw_values = payload.get("disabled_profile_ids") if isinstance(payload, dict) else None
    if not isinstance(raw_values, list):
        return set()
    return {
        str(profile_id).strip()
        for profile_id in raw_values
        if str(profile_id).strip()
    }


def _optional_int(value: object | None) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _profile_tier_sort_key(
    profile: dict,
    tier: str,
    candidates: list[dict],
) -> tuple[float, float]:
    if tier != PROFILE_TIER_BALANCED:
        return (0.0, _profile_sort_time(profile))
    return (
        _balanced_profile_score(profile, candidates),
        _profile_sort_time(profile),
    )


def _balanced_profile_score(profile: dict, candidates: list[dict]) -> float:
    best_fpsw = max(
        (_optional_float(candidate.get("efficiency_fps_per_w")) or 0.0)
        for candidate in candidates
    )
    best_fps = max(
        (_optional_float(candidate.get("avg_fps")) or 0.0)
        for candidate in candidates
    )
    fpsw = _optional_float(profile.get("efficiency_fps_per_w"))
    fps = _optional_float(profile.get("avg_fps"))
    fpsw_score = _relative_score(fpsw, best_fpsw)
    fps_score = _relative_score(fps, best_fps)
    return (fpsw_score + fps_score) / 2.0


def _relative_score(value: float | None, best: float) -> float:
    if value is None or best <= 0.0:
        return 0.0
    return max(0.0, float(value) / float(best))


def _optional_float(value: object | None) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _truthy(value: object | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


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
        return time.time()
