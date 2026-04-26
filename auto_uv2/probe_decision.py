from __future__ import annotations

from dataclasses import dataclass

from auto_uv.scan_rules import _final_failure_can_accept_budget_curve

from .candidate_decision import AutoUv2OverclockBudget


LOW_CLOCK_FAILURE_PREFIXES = (
    "telemetry-live-core_clock",
    "telemetry-live-core_clock-avg",
)

HARD_RECOVERY_FAILURE_PREFIXES = (
    "cuda",
    "q2rtx",
    "timedemo",
    "stability",
    "fps-regression",
    "frame-count-regression",
)


@dataclass(frozen=True, slots=True)
class AutoUv2ProbeDecision:
    action: str
    should_back_off_overclock: bool
    reason: str


def _is_low_clock_failure(reason: str | None) -> bool:
    return str(reason or "").startswith(LOW_CLOCK_FAILURE_PREFIXES)


def _is_hard_recovery_failure(reason: str | None) -> bool:
    return str(reason or "").startswith(HARD_RECOVERY_FAILURE_PREFIXES)


def classify_probe_result(
    *,
    probe_success: bool,
    probe_failure_reason: str | None,
    evaluation_error: str | None,
    budget: AutoUv2OverclockBudget,
    candidate_used_overclock: bool,
) -> AutoUv2ProbeDecision:
    reason = str(evaluation_error or probe_failure_reason or "")

    if not probe_success:
        # Low clock is recoverable; CUDA/Q2RTX failures recover upward.
        if _is_low_clock_failure(reason):
            if not budget.spent_or_disabled:
                return AutoUv2ProbeDecision(
                    "try-overclock",
                    False,
                    "probe only missed the loaded-clock floor",
                )
            return AutoUv2ProbeDecision(
                "accept-lowest-floor-miss",
                False,
                "clock floor missed after overclock budget was spent",
            )
        return AutoUv2ProbeDecision(
            "recover-upward",
            bool(candidate_used_overclock or _is_hard_recovery_failure(reason)),
            f"probe failed: {reason or 'unknown'}",
        )

    if not reason:
        return AutoUv2ProbeDecision("accept", False, "probe passed")

    # A completed workload with only a floor miss can be the last good point.
    if _final_failure_can_accept_budget_curve(reason):
        return AutoUv2ProbeDecision(
            "accept-lowest-floor-miss",
            False,
            f"passed workload but missed clock floor: {reason}",
        )

    return AutoUv2ProbeDecision(
        "reject-guardrail",
        bool(candidate_used_overclock or _is_hard_recovery_failure(reason)),
        f"probe passed workload but failed guardrail: {reason}",
    )
