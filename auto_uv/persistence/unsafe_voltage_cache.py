"""Interpret persisted unsafe-voltage entries from previous Auto-UV runs.

The cache blocks known-crashing voltage/clock bands but keeps controlled low-clock exits testable.
"""

from __future__ import annotations

from profiles.uv.profile_tiers import (
    PROFILE_TIER_BALANCED,
    PROFILE_TIER_EFFICIENCY,
    PROFILE_TIER_PERFORMANCE,
    generated_profile_tier,
    normalize_profile_tier,
)

from ..curve.base_vf_curve_voltage_bins import next_higher_editable_voltage_bin
from ..shared.positive_int import positive_int

PROFILE_TIER_KEYS = (
    "generated_profile_tier",
    "profile_generated_tier",
    "auto_uv_requested_mode",
    "auto_uv_profile_tier",
    "profile_tier",
)

KNOWN_PROFILE_TIERS = {
    PROFILE_TIER_EFFICIENCY,
    PROFILE_TIER_BALANCED,
    PROFILE_TIER_PERFORMANCE,
}


CONTROLLED_CLOCK_FLOOR_FAILURE_PREFIXES = (
    "telemetry-live-core_clock",
    "telemetry-live-core_clock-avg",
    "core_clock-regression",
)

CONTROLLED_TERMINATION_FAILURE_PREFIXES = (
    "user-stop-requested",
    "cuda-bruteforce-failed exit=-15",
    # Q2RTX failed to launch because of a missing runtime library - an environment error,
    # not voltage instability. Matches stability.q2rtx Q2RTX_LAUNCHER_ERROR_REASON.
    "q2rtx-launcher-error",
)


def unsafe_min_search_voltage(
    base_curve: list[dict],
    *,
    start_voltage_mv: int,
    unsafe_entries: list[dict],
    profile_tier: object | None = None,
) -> tuple[int | None, int | None]:
    unsafe_floor_mv = None
    for entry in unsafe_entries:
        if not unsafe_entry_blocks_future_search(entry, profile_tier=profile_tier):
            continue
        voltage_mv = positive_int(entry.get("candidate_voltage_mv"))
        lock_clock_mhz = positive_int(entry.get("lock_clock_mhz"))
        if voltage_mv is None or lock_clock_mhz is not None:
            continue
        if isinstance(entry.get("blocked_lock_clock_mhz"), list):
            continue
        if int(voltage_mv) >= int(start_voltage_mv):
            continue
        if unsafe_floor_mv is None or int(voltage_mv) > int(unsafe_floor_mv):
            unsafe_floor_mv = int(voltage_mv)

    if unsafe_floor_mv is None:
        return None, None
    return unsafe_floor_mv, next_higher_editable_voltage_bin(base_curve, unsafe_floor_mv)


def unsafe_entry_blocks_voltage_candidate(
    entry: dict,
    *,
    candidate_voltage_mv: int,
    lock_clock_mhz: int,
    profile_tier: object | None = None,
) -> bool:
    if not unsafe_entry_blocks_future_search(entry, profile_tier=profile_tier):
        return False
    unsafe_voltage_mv = positive_int(entry.get("candidate_voltage_mv"))
    if unsafe_voltage_mv is None:
        return False
    unsafe_lock_clock_mhz = positive_int(entry.get("lock_clock_mhz")) or 0
    clock_floor_mhz = unsafe_entry_clock_floor_mhz(
        entry,
        fallback_lock_clock_mhz=int(unsafe_lock_clock_mhz),
    )

    # Legacy entries without a clock block this voltage and everything below it.
    if unsafe_lock_clock_mhz <= 0:
        return int(candidate_voltage_mv) <= int(unsafe_voltage_mv)
    return int(candidate_voltage_mv) <= int(unsafe_voltage_mv) and int(
        lock_clock_mhz
    ) >= int(clock_floor_mhz)


def unsafe_voltage_block_reason(
    unsafe_entries: list[dict],
    *,
    candidate_voltage_mv: int,
    lock_clock_mhz: int,
    profile_tier: object | None = None,
) -> str:
    for entry in unsafe_entries:
        if not unsafe_entry_blocks_voltage_candidate(
            entry,
            candidate_voltage_mv=int(candidate_voltage_mv),
            lock_clock_mhz=int(lock_clock_mhz),
            profile_tier=profile_tier,
        ):
            continue
        unsafe_voltage_mv = positive_int(entry.get("candidate_voltage_mv"))
        unsafe_clock_mhz = positive_int(entry.get("lock_clock_mhz"))
        clock_floor_mhz = unsafe_entry_clock_floor_mhz(
            entry,
            fallback_lock_clock_mhz=int(unsafe_clock_mhz or 0),
        )
        clock_text = f"{int(unsafe_clock_mhz)}MHz" if unsafe_clock_mhz else "unknown"
        band_text = (
            f" band>={int(clock_floor_mhz)}MHz"
            if clock_floor_mhz > 0 and unsafe_clock_mhz
            else ""
        )
        voltage_text = int(unsafe_voltage_mv)
        return f"cached unsafe point {voltage_text}mV@{clock_text}{band_text}"
    return ""


def unsafe_entry_clock_floor_mhz(
    entry: dict,
    *,
    fallback_lock_clock_mhz: int,
) -> int:
    clocks = []
    blocked = entry.get("blocked_lock_clock_mhz")
    if isinstance(blocked, list):
        for value in blocked:
            parsed = positive_int(value)
            if parsed is not None:
                clocks.append(int(parsed))
    if clocks:
        return min(clocks)
    return max(0, int(fallback_lock_clock_mhz))


def unsafe_entry_blocks_future_search(
    entry: dict,
    *,
    profile_tier: object | None = None,
) -> bool:
    if not isinstance(entry, dict):
        return False
    if not unsafe_entry_matches_profile_tier(entry, profile_tier=profile_tier):
        return False
    return not any(
        controlled_failure_reason(reason)
        for reason in unsafe_entry_reason_values(entry)
    )


def unsafe_entry_reason_values(entry: dict) -> list[str]:
    reasons = [str(entry.get("reason") or "")]
    details = entry.get("details")
    if isinstance(details, dict):
        reasons.extend(
            str(details.get(key) or "")
            for key in ("result_reason", "shutdown_mode")
        )
    return [reason for reason in reasons if reason]


def controlled_failure_reason(reason: str | None) -> bool:
    text = str(reason or "")
    return text.startswith(
        CONTROLLED_CLOCK_FLOOR_FAILURE_PREFIXES
        + CONTROLLED_TERMINATION_FAILURE_PREFIXES
    )


def unsafe_entry_matches_profile_tier(
    entry: dict,
    *,
    profile_tier: object | None,
) -> bool:
    current_tier = cache_profile_tier(profile_tier)
    if not current_tier:
        return True
    entry_tier = unsafe_entry_profile_tier(entry)
    return not entry_tier or entry_tier == current_tier


def unsafe_entry_profile_tier(entry: dict) -> str:
    for payload in unsafe_entry_profile_tier_payloads(entry):
        tier = explicit_cache_profile_tier(payload)
        if tier:
            return tier
    return ""


def unsafe_entry_profile_tier_payloads(entry: dict):
    if not isinstance(entry, dict):
        return
    yield entry
    details = entry.get("details")
    if isinstance(details, dict):
        yield details
        marker_details = details.get("marker_details")
        if isinstance(marker_details, dict):
            yield marker_details


def explicit_cache_profile_tier(payload: dict) -> str:
    for key in PROFILE_TIER_KEYS:
        tier = cache_profile_tier(payload.get(key))
        if tier:
            return tier

    mode = cache_profile_tier(payload.get("auto_uv_mode"))
    if mode:
        return mode

    if adaptive_scan_payload(payload):
        return ""

    tail_rise_bins = int_or_none(payload.get("tail_rise_bins"))
    if tail_rise_bins is not None:
        return generated_profile_tier({"tail_rise_bins": int(tail_rise_bins)})

    return ""


def adaptive_scan_payload(payload: dict) -> bool:
    # Adaptive scans sweep all three tiers in one run, so their markers stay
    # tier-agnostic instead of letting flat tail bins read as efficiency.
    return any(
        str(payload.get(key) or "").strip().lower() == "adaptive"
        for key in ("auto_uv_requested_mode", "auto_uv_mode")
    )


def cache_profile_tier(value: object | None) -> str:
    tier = normalize_profile_tier(value)
    if tier:
        return tier

    text = str(value or "").strip().lower()
    if text.startswith("efficiency"):
        return PROFILE_TIER_EFFICIENCY
    if text in KNOWN_PROFILE_TIERS:
        return text
    return ""


def int_or_none(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
