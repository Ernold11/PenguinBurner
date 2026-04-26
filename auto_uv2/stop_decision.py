from __future__ import annotations

from dataclasses import dataclass

from .candidate_decision import AutoUv2OverclockBudget


@dataclass(frozen=True, slots=True)
class AutoUv2EfficiencyStop:
    should_stop: bool
    confirmations: int
    use_current_curve: bool
    reason: str


def decide_efficiency_stop(
    *,
    efficiency_stop_candidate: bool,
    voltage_drop_from_start_pct: float,
    min_voltage_drop_pct: float,
    no_gain_streak: int,
    required_extra_confirmations: int,
    pending_previous_curve: bool,
    budget: AutoUv2OverclockBudget,
    power_up_efficiency_down: bool,
    efficiency_delta_pct: float | None,
) -> AutoUv2EfficiencyStop:
    if not efficiency_stop_candidate:
        return AutoUv2EfficiencyStop(False, 0, False, "efficiency still improving")
    # Do not stop on marginal FPS/W before the requested search depth.
    if float(voltage_drop_from_start_pct) < float(min_voltage_drop_pct):
        return AutoUv2EfficiencyStop(False, 0, False, "minimum voltage drop not reached")
    if not pending_previous_curve:
        return AutoUv2EfficiencyStop(False, 0, False, "first no-gain probe arms stop")
    confirmations = max(0, int(no_gain_streak) - 1)
    if int(no_gain_streak) <= int(required_extra_confirmations):
        return AutoUv2EfficiencyStop(False, confirmations, False, "confirming no-gain")
    if not budget.spent_or_disabled:
        return AutoUv2EfficiencyStop(False, confirmations, False, "budget still available")

    # Keep the current curve only for tiny non-negative gains, never for a hard regression.
    use_current = (
        not bool(power_up_efficiency_down)
        and efficiency_delta_pct is not None
        and float(efficiency_delta_pct) >= 0.0
    )
    return AutoUv2EfficiencyStop(
        True,
        confirmations,
        use_current,
        "fps-per-watt wall reached",
    )
