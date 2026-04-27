from __future__ import annotations


CONTROLLED_CLOCK_FLOOR_FAILURE_PREFIXES = (
    "telemetry-live-core_clock",
    "telemetry-live-core_clock-avg",
)

CONTROLLED_TERMINATION_FAILURE_PREFIXES = (
    "user-stop-requested",
    "cuda-bruteforce-failed exit=-15",
)


def _is_controlled_clock_floor_failure(reason: str | None) -> bool:
    return str(reason or "").startswith(CONTROLLED_CLOCK_FLOOR_FAILURE_PREFIXES)


def _is_controlled_probe_termination(reason: str | None) -> bool:
    return str(reason or "").startswith(CONTROLLED_TERMINATION_FAILURE_PREFIXES)


def _is_non_hard_probe_failure(reason: str | None) -> bool:
    return _is_controlled_clock_floor_failure(
        reason
    ) or _is_controlled_probe_termination(reason)


def _unsafe_entry_reason_values(entry: dict) -> list[str]:
    reasons = [str(entry.get("reason") or "")]
    details = entry.get("details")
    if isinstance(details, dict):
        reasons.extend(
            str(details.get(key) or "")
            for key in ("result_reason", "shutdown_mode")
        )
    return [reason for reason in reasons if reason]


def _unsafe_entry_blocks_future_search(entry: dict) -> bool:
    if not isinstance(entry, dict):
        return False
    return not any(
        _is_non_hard_probe_failure(reason)
        for reason in _unsafe_entry_reason_values(entry)
    )
