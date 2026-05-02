"""Translate one Auto-UV3 voltage probe into the JSON events the UI table expects.

The scan emits these events around Q2RTX/CUDA probes so the UI can create a row,
stream live telemetry into it, then replace it with the final measured result.
"""

from __future__ import annotations

import re

from ..auto_uv_types import VfCurveCandidate
from .clock_recovery_budget_ui_payload import clock_recovery_budget_ui_payload
from .probe_summary_ui_payload import probe_summary_ui_payload
from .ui_json_event_writer import AutoUvEventCallback, emit_ui_json_event
from .vf_curve_ui_points import vf_curve_ui_points
from ..voltage_sweep_state import VoltageProbeOutcome


RECOVERY_BUDGET_RE = re.compile(
    r"\brecovery-budget=(?P<used>[0-9]+(?:\.[0-9]+)?)/"
    r"(?P<limit>[0-9]+(?:\.[0-9]+)?)%"
)


def emit_ui_voltage_probe_started(
    event_callback: AutoUvEventCallback | None,
    candidate: VfCurveCandidate,
    *,
    stage: str,
    max_clock_drop_pct: float | int | None = None,
) -> None:
    identity = ui_voltage_probe_identity(
        candidate,
        stage=stage,
        max_clock_drop_pct=max_clock_drop_pct,
    )
    emit_ui_json_event(
        event_callback,
        "candidate_curve",
        **identity,
        points=vf_curve_ui_points(candidate.flattened_plan),
    )
    emit_ui_json_event(event_callback, "probe_start", **identity)


def emit_ui_voltage_probe_finished(
    event_callback: AutoUvEventCallback | None,
    candidate: VfCurveCandidate,
    outcome: VoltageProbeOutcome,
    *,
    stage: str,
    max_clock_drop_pct: float | int | None = None,
) -> None:
    emit_ui_json_event(
        event_callback,
        "probe_result",
        **ui_voltage_probe_result_payload(
            candidate,
            outcome,
            stage=stage,
            max_clock_drop_pct=max_clock_drop_pct,
        ),
    )


def ui_voltage_probe_identity(
    candidate: VfCurveCandidate,
    *,
    stage: str,
    max_clock_drop_pct: float | int | None = None,
) -> dict:
    payload = {
        "stage": str(stage),
        "voltage_mv": int(candidate.voltage_mv),
        "clock_mhz": int(candidate.target_mhz),
        "label": str(candidate.label),
    }
    payload.update(
        ui_clock_recovery_budget_payload(
            candidate.label,
            max_clock_drop_pct=max_clock_drop_pct,
        )
    )
    return payload


def ui_voltage_probe_result_payload(
    candidate: VfCurveCandidate,
    outcome: VoltageProbeOutcome,
    *,
    stage: str,
    max_clock_drop_pct: float | int | None = None,
) -> dict:
    decision = outcome.decision
    budget_payload = ui_clock_recovery_budget_payload(
        candidate.label,
        max_clock_drop_pct=max_clock_drop_pct,
    )
    if outcome.raw_probe is not None:
        payload = probe_summary_ui_payload(
            outcome.raw_probe,
            stage=str(stage),
            decision="pass" if bool(decision.passed) else "fail",
            reason=str(decision.reason or decision.failure_kind.value),
        )
        payload.update(ui_probe_decision_payload(decision, result=outcome.raw_result))
        payload.update(budget_payload)
        return payload
    payload = {
        **ui_voltage_probe_identity(
            candidate,
            stage=stage,
            max_clock_drop_pct=max_clock_drop_pct,
        ),
        "measured_clock_mhz": _rounded(outcome.measured_core_clock_mhz),
        "avg_voltage_mv": _rounded(outcome.measured_voltage_mv),
        "decision": "pass" if bool(decision.passed) else "fail",
        "reason": str(decision.reason or decision.failure_kind.value),
    }
    payload.update(ui_probe_decision_payload(decision, result=outcome.raw_result))
    payload.update(budget_payload)
    return payload


def ui_probe_decision_payload(decision, *, result=None) -> dict:
    return {
        "failure_kind": str(decision.failure_kind.value),
        "failure_severity": str(decision.severity.value),
        "failure_evidence": dict(decision.evidence or {}),
        "fatal_output_matches": list(getattr(result, "fatal_output_matches", []) or []),
    }


def ui_clock_recovery_budget_payload(
    label: str,
    *,
    max_clock_drop_pct: float | int | None = None,
) -> dict:
    match = RECOVERY_BUDGET_RE.search(str(label or ""))
    if match is None:
        return {}
    return clock_recovery_budget_ui_payload(
        used_pct=float(match.group("used")),
        limit_pct=float(match.group("limit")),
        max_clock_drop_pct=max_clock_drop_pct,
    )


def _rounded(value: float | int | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 2)
