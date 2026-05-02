"""Abort verification when a loaded profile misses its target voltage under load.

The rule ignores warmup, idle samples, and isolated spikes; only sustained loaded drift fails.
"""

from __future__ import annotations


PROFILE_VERIFY_VOLTAGE_TOLERANCE_MV = 50
PROFILE_VERIFY_VOLTAGE_MISMATCH_STREAK = 5
PROFILE_VERIFY_VOLTAGE_WARMUP_S = 8.0


def profile_verification_voltage_abort_callback(
    flatten_target: dict,
    *,
    previous_callback=None,
):
    target_voltage_mv = _coerce_positive_int(
        dict(flatten_target).get("lock_voltage_mv")
    )
    if target_voltage_mv is None:
        return previous_callback
    tolerance_mv = int(PROFILE_VERIFY_VOLTAGE_TOLERANCE_MV)
    state = {"high_voltage_streak": 0}

    def abort_callback(progress_state: dict) -> str | None:
        if previous_callback is not None:
            reason = previous_callback(progress_state)
            if reason:
                return str(reason)
        elapsed_s = _progress_elapsed_s(progress_state)
        if elapsed_s < float(PROFILE_VERIFY_VOLTAGE_WARMUP_S):
            state["high_voltage_streak"] = 0
            return None
        sample = progress_state.get("latest_sample")
        voltage_mv = _float_or_none(getattr(sample, "voltage_mv", None))
        if voltage_mv is None:
            state["high_voltage_streak"] = 0
            return None
        gpu_util_pct = _float_or_none(getattr(sample, "gpu_util_pct", None))
        if gpu_util_pct is not None and gpu_util_pct < 50.0:
            state["high_voltage_streak"] = 0
            return None
        if voltage_mv <= float(target_voltage_mv + tolerance_mv):
            state["high_voltage_streak"] = 0
            return None
        state["high_voltage_streak"] = int(state["high_voltage_streak"]) + 1
        if state["high_voltage_streak"] < int(
            PROFILE_VERIFY_VOLTAGE_MISMATCH_STREAK
        ):
            return None
        return (
            "profile-verification-voltage-mismatch "
            f"current={voltage_mv:.0f}mV "
            f"target={int(target_voltage_mv)}mV "
            f"tolerance={int(tolerance_mv)}mV "
            f"streak={int(state['high_voltage_streak'])}"
        )

    return abort_callback


def _progress_elapsed_s(progress_state: dict) -> float:
    return _float_or_none(
        progress_state.get("progress_elapsed_s", progress_state.get("elapsed_s", 0.0))
    ) or 0.0


def _coerce_positive_int(value) -> int | None:
    number = _float_or_none(value)
    if number is None:
        return None
    integer = int(round(number))
    return integer if integer > 0 else None


def _float_or_none(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
