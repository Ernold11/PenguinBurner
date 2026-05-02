from __future__ import annotations

from auto_uv3.auto_uv_types import (
    FailureKind,
    FailureSeverity,
    StableRunDecision,
    VfCurveCandidate,
)
from auto_uv3.ui.ui_voltage_probe_events import (
    emit_ui_voltage_probe_finished,
    emit_ui_voltage_probe_started,
)
from auto_uv3.voltage_sweep_state import VoltageProbeOutcome
from auto_uv3_test_data import base_curve


def test_ui_probe_start_events_create_curve_and_table_row() -> None:
    events: list[tuple[str, dict]] = []
    candidate = VfCurveCandidate(
        label="lower-voltage recovery-budget=0.60/1.20%",
        voltage_mv=950,
        target_mhz=2400,
        flattened_plan=base_curve(900, 1000, 25, 2200, 20),
    )

    emit_ui_voltage_probe_started(
        lambda name, payload: events.append((name, payload)),
        candidate,
        stage="candidate",
        max_clock_drop_pct=10.0,
    )

    assert [name for name, _payload in events] == ["candidate_curve", "probe_start"]
    assert events[1][1] == {
        "stage": "candidate",
        "voltage_mv": 950,
        "clock_mhz": 2400,
        "label": "lower-voltage recovery-budget=0.60/1.20%",
        "overclock_budget_clock_drop_pct": 10.0,
        "overclock_budget_limit_of_clock_drop_pct": 12.0,
        "overclock_budget_limit_pct": 1.2,
        "overclock_budget_used_of_clock_drop_pct": 6.0,
        "overclock_budget_used_pct": 0.6,
        "overclock_budget_used_ratio": 0.5,
    }
    assert len(events[0][1]["points"]) == 4


def test_ui_probe_result_payload_contains_measured_table_values() -> None:
    events: list[tuple[str, dict]] = []
    candidate = VfCurveCandidate(
        label="lower-voltage",
        voltage_mv=950,
        target_mhz=2400,
        flattened_plan=[],
    )
    outcome = VoltageProbeOutcome(
        decision=StableRunDecision(
            passed=False,
            failure_kind=FailureKind.LOW_CLOCK,
            severity=FailureSeverity.RECOVERABLE,
            reason="clock floor miss",
        ),
        measured_core_clock_mhz=2325.4,
        measured_voltage_mv=947.8,
    )

    emit_ui_voltage_probe_finished(
        lambda name, payload: events.append((name, payload)),
        candidate,
        outcome,
        stage="candidate",
    )

    assert events == [
        (
            "probe_result",
            {
                "stage": "candidate",
                "voltage_mv": 950,
                "clock_mhz": 2400,
                "label": "lower-voltage",
                "measured_clock_mhz": 2325.4,
                "avg_voltage_mv": 947.8,
                "decision": "fail",
                "failure_evidence": {},
                "failure_kind": "low-clock",
                "failure_severity": "recoverable",
                "fatal_output_matches": [],
                "reason": "clock floor miss",
            },
        )
    ]


def test_ui_probe_result_payload_converts_recovery_budget_for_table() -> None:
    events: list[tuple[str, dict]] = []
    candidate = VfCurveCandidate(
        label="lower-voltage recovery-budget=1.20/1.20%",
        voltage_mv=935,
        target_mhz=2520,
        flattened_plan=[],
    )
    outcome = VoltageProbeOutcome(
        decision=StableRunDecision(
            passed=True,
            failure_kind=FailureKind.NONE,
            severity=FailureSeverity.PASS,
            reason="stable run",
        ),
        measured_core_clock_mhz=2500.0,
        measured_voltage_mv=935.0,
    )

    emit_ui_voltage_probe_finished(
        lambda name, payload: events.append((name, payload)),
        candidate,
        outcome,
        stage="candidate",
        max_clock_drop_pct=10.0,
    )

    payload = events[0][1]
    assert payload["overclock_budget_used_pct"] == 1.2
    assert payload["overclock_budget_limit_pct"] == 1.2
    assert payload["overclock_budget_used_of_clock_drop_pct"] == 12.0
    assert payload["overclock_budget_limit_of_clock_drop_pct"] == 12.0
