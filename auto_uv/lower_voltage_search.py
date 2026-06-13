"""Choose the next lower voltage bin to probe after a stable point.

This module owns voltage descent speed, effective-voltage spacing, and the final low-bin probe.
"""

from __future__ import annotations

from dataclasses import dataclass

from .curve.base_vf_curve_voltage_bins import lower_editable_voltage_bins
from .shared.probe_data_fields import percent


@dataclass(frozen=True, slots=True)
class LowerVoltageSearchRules:
    aggressive_max_step_bins: int = 3
    aggressive_min_step_bins: int = 2
    fine_step_bins: int = 1
    medium_voltage_pct: float = 90.0
    upper_voltage_nominal_drop_mv: int = 15
    upper_voltage_effective_gap_mv: float = 20.0
    lower_voltage_nominal_drop_mv: int = 5
    lower_voltage_effective_gap_mv: float = 5.0


def select_next_lower_voltage(
    base_curve: list[dict],
    *,
    start_voltage_mv: int,
    stable_voltage_mv: int,
    reference_actual_voltage_mv: float | None,
    preserve_base_below_mv: int | None,
    min_search_voltage_mv: int | None,
    failed_floor_voltage_mv: int | None = None,
    rules: LowerVoltageSearchRules = LowerVoltageSearchRules(),
) -> int | None:
    bins = lower_editable_voltage_bins(
        base_curve,
        int(stable_voltage_mv),
        preserve_base_below_mv=preserve_base_below_mv,
        min_search_voltage_mv=min_search_voltage_mv,
    )
    if failed_floor_voltage_mv is not None:
        bins = [
            int(voltage_mv)
            for voltage_mv in bins
            if int(voltage_mv) > int(failed_floor_voltage_mv)
        ]
    if not bins:
        return None

    selected = select_aggressive_voltage_bins(
        bins,
        start_voltage_mv=int(start_voltage_mv),
        rules=rules,
    )
    selected = filter_effective_voltage_candidates(
        selected,
        stable_voltage_mv=int(stable_voltage_mv),
        reference_actual_voltage_mv=reference_actual_voltage_mv,
        rules=rules,
    )
    # Every candidate bin is drawn from below stable_voltage_mv, so the first
    # survivor is the next voltage to probe.
    return int(selected[0]) if selected else None


def select_aggressive_voltage_bins(
    bins_descending: list[int],
    *,
    start_voltage_mv: int,
    rules: LowerVoltageSearchRules = LowerVoltageSearchRules(),
) -> list[int]:
    selected = []
    cursor = 0
    while cursor < len(bins_descending):
        ratio = (
            float(bins_descending[cursor]) / float(start_voltage_mv)
            if int(start_voltage_mv) > 0
            else 1.0
        )
        if ratio >= percent(rules.medium_voltage_pct):
            step_bins = min(
                int(rules.aggressive_max_step_bins),
                max(
                    int(rules.aggressive_min_step_bins),
                    len(bins_descending) - cursor,
                ),
            )
        else:
            step_bins = int(rules.fine_step_bins)
        pick_index = min(cursor + max(1, step_bins) - 1, len(bins_descending) - 1)
        selected.append(int(bins_descending[pick_index]))
        cursor = int(pick_index) + 1

    # The clamped pick_index always lands on the last bin before the loop exits,
    # so the final bin is already selected here.
    return selected


def filter_effective_voltage_candidates(
    bins_descending: list[int],
    *,
    stable_voltage_mv: int,
    reference_actual_voltage_mv: float | None,
    rules: LowerVoltageSearchRules = LowerVoltageSearchRules(),
) -> list[int]:
    if not bins_descending:
        return []
    if reference_actual_voltage_mv is None:
        return list(bins_descending)

    filtered = []
    reference_mv = float(reference_actual_voltage_mv)
    last_kept_mv = int(stable_voltage_mv)
    for voltage_mv in bins_descending:
        ratio = (
            float(voltage_mv) / float(stable_voltage_mv)
            if int(stable_voltage_mv) > 0
            else 1.0
        )
        if ratio >= percent(rules.medium_voltage_pct):
            min_nominal_drop_mv = int(rules.upper_voltage_nominal_drop_mv)
            min_effective_gap_mv = float(rules.upper_voltage_effective_gap_mv)
        else:
            min_nominal_drop_mv = int(rules.lower_voltage_nominal_drop_mv)
            min_effective_gap_mv = float(rules.lower_voltage_effective_gap_mv)
        effective_gap_mv = abs(float(voltage_mv) - reference_mv)
        nominal_drop_mv = int(last_kept_mv) - int(voltage_mv)
        if (
            not filtered
            or nominal_drop_mv >= min_nominal_drop_mv
            or effective_gap_mv >= min_effective_gap_mv
        ):
            filtered.append(int(voltage_mv))
            last_kept_mv = int(voltage_mv)

    final_voltage_mv = int(bins_descending[-1])
    if final_voltage_mv not in filtered:
        filtered.append(final_voltage_mv)
    return filtered
