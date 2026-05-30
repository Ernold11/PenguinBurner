"""Build a full performance-sweep V/F profile from passed probe anchors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..auto_uv_types import AutoUvProbeSummary, VfCurveCandidate
from ..voltage_sweep_state import VoltageProbeOutcome
from .vf_curve_flattening import FlatteningRules, snap_target_clock


@dataclass(frozen=True, slots=True)
class SweepProfileAnchor:
    voltage_mv: int
    target_mhz: int
    source: str


def build_performance_sweep_profile_candidate(
    base_curve: list[dict],
    *,
    selected_candidate: VfCurveCandidate,
    stable_history: list[AutoUvProbeSummary] | None,
    auto_oc_attempts: Iterable[object] | None,
    rules: FlatteningRules = FlatteningRules(),
) -> VfCurveCandidate:
    anchors = performance_sweep_profile_anchors(
        selected_candidate=selected_candidate,
        stable_history=stable_history,
        auto_oc_attempts=auto_oc_attempts,
    )
    if len(anchors) < 2:
        return selected_candidate

    plan = build_sweep_profile_plan(
        selected_plan=selected_candidate.flattened_plan or list(base_curve),
        anchors=anchors,
        selected_voltage_mv=int(selected_candidate.voltage_mv),
        rules=rules,
    )
    metadata = dict(selected_candidate.metadata)
    metadata["profile_curve"] = "performance-sweep"
    metadata["profile_curve_anchor_count"] = len(anchors)
    metadata["profile_curve_start_voltage_mv"] = int(anchors[0].voltage_mv)
    return VfCurveCandidate(
        label=f"{selected_candidate.label} performance-sweep",
        voltage_mv=int(selected_candidate.voltage_mv),
        target_mhz=int(selected_candidate.target_mhz),
        flattened_plan=plan,
        metadata=metadata,
    )


def performance_sweep_profile_anchors(
    *,
    selected_candidate: VfCurveCandidate,
    stable_history: list[AutoUvProbeSummary] | None,
    auto_oc_attempts: Iterable[object] | None,
) -> list[SweepProfileAnchor]:
    selected_voltage = int(selected_candidate.voltage_mv)
    selected_anchor = SweepProfileAnchor(
        voltage_mv=selected_voltage,
        target_mhz=int(selected_candidate.target_mhz),
        source="selected",
    )
    auto_oc_anchors = [
        SweepProfileAnchor(
            voltage_mv=int(attempt.candidate.voltage_mv),
            target_mhz=int(attempt.candidate.target_mhz),
            source="auto-oc",
        )
        for attempt in list(auto_oc_attempts or [])
        if passed_attempt(attempt)
        and int(attempt.candidate.voltage_mv) <= selected_voltage
    ]
    if not auto_oc_anchors:
        return [selected_anchor]

    first_auto_oc_voltage = min(anchor.voltage_mv for anchor in auto_oc_anchors)
    lower_anchor = lowest_stable_anchor_below(
        stable_history,
        voltage_mv=int(first_auto_oc_voltage),
    )
    anchors = []
    if lower_anchor is not None:
        anchors.append(lower_anchor)
    anchors.extend(auto_oc_anchors)
    anchors.append(selected_anchor)
    return monotonic_anchors(dedupe_anchors_by_voltage(anchors))


def build_sweep_profile_plan(
    *,
    selected_plan: list[dict],
    anchors: list[SweepProfileAnchor],
    selected_voltage_mv: int,
    rules: FlatteningRules = FlatteningRules(),
) -> list[dict]:
    sorted_anchors = sorted(anchors, key=lambda anchor: int(anchor.voltage_mv))
    first_voltage = int(sorted_anchors[0].voltage_mv)
    selected_voltage = int(selected_voltage_mv)
    plan = []
    for raw in selected_plan:
        point = dict(raw)
        voltage_mv = int(point["voltage_mv"])
        base_mhz = int(point["base_mhz"])
        if bool(point.get("preserve_base")):
            target_mhz = base_mhz
        elif first_voltage <= voltage_mv <= selected_voltage:
            target_mhz = interpolated_anchor_clock(
                sorted_anchors,
                voltage_mv=int(voltage_mv),
                rules=rules,
            )
        else:
            target_mhz = int(point["target_mhz"])
        point["target_mhz"] = int(target_mhz)
        point["new_offset_mhz"] = int(target_mhz) - int(base_mhz)
        plan.append(point)
    return plan


def interpolated_anchor_clock(
    anchors: list[SweepProfileAnchor],
    *,
    voltage_mv: int,
    rules: FlatteningRules,
) -> int:
    voltage = int(voltage_mv)
    if voltage <= int(anchors[0].voltage_mv):
        return int(anchors[0].target_mhz)
    for left, right in zip(anchors, anchors[1:]):
        left_voltage = int(left.voltage_mv)
        right_voltage = int(right.voltage_mv)
        if voltage == right_voltage:
            return int(right.target_mhz)
        if left_voltage <= voltage < right_voltage:
            span_mv = max(1, right_voltage - left_voltage)
            fraction = (float(voltage) - float(left_voltage)) / float(span_mv)
            requested = int(
                round(
                    float(left.target_mhz)
                    + ((float(right.target_mhz) - float(left.target_mhz)) * fraction)
                )
            )
            return snap_target_clock(requested, rules=rules)
    return int(anchors[-1].target_mhz)


def lowest_stable_anchor_below(
    stable_history: list[AutoUvProbeSummary] | None,
    *,
    voltage_mv: int,
) -> SweepProfileAnchor | None:
    probes = [
        probe
        for probe in list(stable_history or [])
        if int(probe.candidate_voltage_mv) < int(voltage_mv)
    ]
    if not probes:
        return None
    probe = min(probes, key=lambda item: int(item.candidate_voltage_mv))
    return SweepProfileAnchor(
        voltage_mv=int(probe.candidate_voltage_mv),
        target_mhz=int(probe.lock_clock_mhz),
        source="lower-voltage",
    )


def dedupe_anchors_by_voltage(
    anchors: list[SweepProfileAnchor],
) -> list[SweepProfileAnchor]:
    by_voltage: dict[int, SweepProfileAnchor] = {}
    for anchor in anchors:
        by_voltage[int(anchor.voltage_mv)] = anchor
    return [by_voltage[voltage] for voltage in sorted(by_voltage)]


def monotonic_anchors(anchors: list[SweepProfileAnchor]) -> list[SweepProfileAnchor]:
    normalized = []
    previous_clock = 0
    for anchor in sorted(anchors, key=lambda item: int(item.voltage_mv)):
        target_mhz = max(int(anchor.target_mhz), int(previous_clock))
        normalized.append(
            SweepProfileAnchor(
                voltage_mv=int(anchor.voltage_mv),
                target_mhz=int(target_mhz),
                source=str(anchor.source),
            )
        )
        previous_clock = int(target_mhz)
    return normalized


def passed_attempt(attempt: object) -> bool:
    outcome = getattr(attempt, "outcome", None)
    if not isinstance(outcome, VoltageProbeOutcome):
        return False
    return (
        bool(outcome.decision.passed)
        and getattr(attempt, "candidate", None) is not None
    )
