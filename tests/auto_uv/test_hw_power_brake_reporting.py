"""A power-delivery brake must never pass as an ordinary capped run.

``hw-power-brake`` is the board's protection circuit (EDPp/OCP), not the
configured power limit. It still earns the power-walled clock exemption —
the clock loss really is caused by power rather than V/F instability — but
the scan reports it on every surface so a brake-heavy run is legible in the
log, the event stream, the saved result, and the decision text itself.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from auto_uv.domain.console_log import hw_power_brake_note, log_benchmark
from auto_uv.persistence.verified_candidate_result_file import probe_metrics
from auto_uv.probes.event_payload import probe_summary_event_payload
from auto_uv.probes.stability_decision import (
    StabilityThresholds,
    evaluate_loaded_telemetry,
)
from auto_uv.probes.summary import (
    count_hw_power_brake_samples,
    summarize_q2rtx_cuda_probe,
)


def _samples(reason: str, *, count: int = 8) -> list[dict]:
    return [
        {
            "elapsed_s": 6.0 + float(index),
            "gpu_util_pct": 99.0,
            "power_w": 300.0,
            "core_clock_mhz": 1900.0,
            "voltage_mv": 900.0,
            "perf_cap_reason": reason,
        }
        for index in range(int(count))
    ]


def _decision(reason: str):
    return evaluate_loaded_telemetry(
        _samples(reason),
        baseline_power_w=300.0,
        baseline_core_clock_mhz=2595.0,
        power_limit_w=430,
        thresholds=StabilityThresholds(min_core_clock_pct=94.0),
        log_path=None,
    )


def _summary(reason: str):
    samples = _samples(reason)
    return summarize_q2rtx_cuda_probe(
        candidate_voltage_mv=900,
        lock_clock_mhz=2595,
        live_voltage_before_mv=900,
        live_voltage_after_mv=900,
        used_companion_load=False,
        power_limit_w=430,
        result=SimpleNamespace(
            telemetry_samples=samples,
            companion_telemetry_samples=[],
            telemetry_summary=lambda: {},
            reason="ok",
            log_path=Path("/tmp/q2rtx.log"),
        ),
    )


def test_brake_samples_are_counted_and_plain_power_caps_are_not() -> None:
    assert count_hw_power_brake_samples(_samples("hw-power-brake", count=5)) == 5
    assert count_hw_power_brake_samples(_samples("sw-power", count=5)) == 0
    # Combined masks are the common real shape and must still count.
    assert count_hw_power_brake_samples(
        _samples("sw-power+hw-power-brake", count=3)
    ) == 3
    assert count_hw_power_brake_samples(_samples("none", count=3)) == 0


def test_power_walled_pass_names_the_brake_in_its_reason() -> None:
    braked = _decision("hw-power-brake")
    capped = _decision("sw-power")

    # The exemption still applies — this is the deliberate behavior.
    assert braked.passed
    assert capped.passed
    # But the brake is never silent.
    assert "hw-power-brake=8/8" in braked.reason
    assert "hw-power-brake" not in capped.reason


def test_probe_summary_reports_brake_coverage() -> None:
    braked = _summary("hw-power-brake")
    capped = _summary("sw-power")

    assert braked.hw_power_brake_samples == 8
    assert braked.telemetry_sample_count == 8
    assert capped.hw_power_brake_samples == 0


def test_brake_reaches_the_event_stream_and_the_saved_result() -> None:
    summary = _summary("hw-power-brake")

    payload = probe_summary_event_payload(summary, stage="candidate")
    metrics = probe_metrics(summary)

    assert payload["hw_power_brake_samples"] == 8
    assert metrics["hw_power_brake_samples"] == 8


def test_benchmark_log_calls_out_the_brake_only_when_it_fired() -> None:
    braked = _summary("hw-power-brake")
    capped = _summary("sw-power")
    lines: list[str] = []

    log_benchmark(lines.append, phase="candidate", probe=braked)
    log_benchmark(lines.append, phase="candidate", probe=capped)

    brake_lines = [line for line in lines if "hw-power-brake" in line]
    assert len(brake_lines) == 1
    assert "8/8" in brake_lines[0]
    assert "not the configured power limit" in brake_lines[0]
    assert hw_power_brake_note(capped) is None
