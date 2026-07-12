from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from auto_uv.domain.types import (
    AutoUvError,
    AutoUvProbeSummary,
    FailureKind,
    FailureSeverity,
    StableRunDecision,
    VfCurveCandidate,
)
from auto_uv.run import baseline_probe
from auto_uv.run import crash_recovery
from auto_uv import main_loop as undervolt_main_loop
from auto_uv import efficiency_uv_loop
from auto_uv import performance_uv_loop
from auto_uv.final_verification.main_loop import (
    run_final_verification_and_save as real_run_final_verification_and_save,
)
from auto_uv.run.voltage_sweep_state import (
    LowerVoltageSweepResult,
    VoltageProbeOutcome,
    VoltageSweepState,
)
from ui.features.auto_uv import candidate_choice as candidate_choice_module
from auto_uv_test_data import base_curve


def _summary(voltage_mv: int, clock_mhz: int) -> AutoUvProbeSummary:
    return AutoUvProbeSummary(
        candidate_voltage_mv=int(voltage_mv),
        lock_clock_mhz=int(clock_mhz),
        live_voltage_before_mv=int(voltage_mv),
        live_voltage_after_mv=int(voltage_mv),
        avg_voltage_mv=float(voltage_mv),
        frames_per_run=1000,
        avg_seconds_per_run=10.0,
        avg_fps=100.0,
        min_fps=100.0,
        max_fps=100.0,
        avg_power_w=200.0,
        max_power_w=210.0,
        avg_temperature_c=60.0,
        max_temperature_c=62.0,
        avg_fan_speed_pct=35.0,
        max_fan_speed_pct=36.0,
        avg_core_clock_mhz=float(clock_mhz),
        efficiency_fps_per_w=0.5,
        efficiency_mhz_per_w=10.0,
        watts_per_mhz=0.1,
        used_companion_load=False,
        result_reason="stable run",
        log_path=Path("/tmp/q2rtx.log"),
    )


def _assert_final_verification_kwargs_match_real_signature(kwargs: dict) -> None:
    accepted = set(inspect.signature(real_run_final_verification_and_save).parameters)
    unexpected = set(kwargs) - accepted

    assert unexpected == set()


def test_discovery_probe_runner_uses_live_voltage_reader_keyword(monkeypatch) -> None:
    captured = {}

    class FakeGpu:
        reader = object()
        live_voltage_reader = object()
        runtime_default_plan = []
        power_limit_w = 390
        baseline_power_limit_w = 360

    class FakeRunner:
        def __init__(self, *, reader, live_voltage_reader, **kwargs):
            captured["reader"] = reader
            captured["live_voltage_reader"] = live_voltage_reader
            captured["kwargs"] = kwargs

        def probe_default_curve(self, *, base_curve, label_voltage_mv, label_clock_mhz):
            captured["base_curve"] = base_curve
            captured["label_voltage_mv"] = label_voltage_mv
            captured["label_clock_mhz"] = label_clock_mhz
            return object(), type("Result", (), {"success": True, "reason": "ok"})()

    monkeypatch.setattr(baseline_probe, "AutoUvProbeRunner", FakeRunner)
    monkeypatch.setattr(
        baseline_probe,
        "emit_auto_uv_event",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        baseline_probe,
        "probe_summary_event_payload",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(baseline_probe, "log_benchmark", lambda *args, **kwargs: None)

    baseline_probe.run_discovery_probe(
        base_curve(900, 1025, 25, 2000, 40),
        gpu=FakeGpu(),
        q2rtx_config=object(),
        short_probe_base_duration_s=10,
        log=lambda _message: None,
        event_callback=None,
    )

    assert captured["live_voltage_reader"] is FakeGpu.live_voltage_reader
    assert captured["label_voltage_mv"] == 1000
    assert captured["kwargs"]["power_limit_w"] == 360


def test_discovery_probe_logs_selected_gpu_light_load_diagnostic(monkeypatch) -> None:
    log_messages: list[str] = []

    class FakeGpu:
        reader = object()
        live_voltage_reader = object()
        runtime_default_plan = []
        power_limit_w = 110

    class FakeRunner:
        def __init__(self, **_kwargs):
            return None

        def probe_default_curve(self, *, base_curve, label_voltage_mv, label_clock_mhz):
            _ = base_curve, label_voltage_mv, label_clock_mhz
            return (
                _summary(1000, 1335),
                SimpleNamespace(
                    success=True,
                    reason="ok",
                    telemetry_samples=[
                        {
                            "elapsed_s": 6.0,
                            "power_w": 30.0,
                            "gpu_util_pct": 97.0,
                            "core_clock_mhz": 1320.0,
                        },
                        {
                            "elapsed_s": 7.0,
                            "power_w": 31.0,
                            "gpu_util_pct": 98.0,
                            "core_clock_mhz": 1335.0,
                        },
                    ],
                ),
            )

    monkeypatch.setattr(baseline_probe, "AutoUvProbeRunner", FakeRunner)
    monkeypatch.setattr(
        baseline_probe,
        "emit_auto_uv_event",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        baseline_probe,
        "probe_summary_event_payload",
        lambda *args, **kwargs: {},
    )

    baseline_probe.run_discovery_probe(
        base_curve(900, 1025, 25, 2000, 40),
        gpu=FakeGpu(),
        q2rtx_config=object(),
        short_probe_base_duration_s=10,
        log=log_messages.append,
        event_callback=None,
    )

    assert any(
        "warning selected NVIDIA GPU light-load diagnostic" in message
        for message in log_messages
    )
    assert any("power_limit=110W" in message for message in log_messages)
    assert any("max_power=31.0W" in message for message in log_messages)


def test_auto_uv_final_choice_runs_before_final_verification(monkeypatch) -> None:
    curve = base_curve(900, 1025, 25, 2000, 40)
    captured: dict[str, object] = {"direct_probe_calls": [], "sweep_calls": []}

    class FakeGpu:
        reader = object()
        live_voltage_reader = object()
        runtime_default_plan = curve
        power_limit_w = 320
        clock_ceiling = None
        translated_gpu_policy = {}

        def start_clock_ceiling(self, _target) -> None:
            return None

        def close(self) -> None:
            return None

    class FakeRunner:
        def __init__(self, **_kwargs):
            return None

        def probe_baseline_candidate(self, candidate):
            summary = _summary(candidate.voltage_mv, candidate.target_mhz)
            summary.avg_fps = 65.0
            summary.efficiency_fps_per_w = 65.0 / 300.0
            return VoltageProbeOutcome(
                decision=StableRunDecision(
                    True,
                    FailureKind.NONE,
                    FailureSeverity.PASS,
                    "stable run",
                ),
                measured_core_clock_mhz=float(candidate.target_mhz),
                measured_voltage_mv=float(candidate.voltage_mv),
                raw_probe=summary,
            )

        def probe_sweep_candidate(self, candidate, *, stable_history, phase_label):
            _ = stable_history, phase_label
            captured["direct_probe_calls"].append(
                (
                    str(candidate.label),
                    int(candidate.voltage_mv),
                    int(candidate.target_mhz),
                )
            )
            return VoltageProbeOutcome(
                decision=StableRunDecision(
                    True,
                    FailureKind.NONE,
                    FailureSeverity.PASS,
                    "stable run",
                ),
                measured_core_clock_mhz=float(candidate.target_mhz),
                measured_voltage_mv=float(candidate.voltage_mv),
                raw_probe=_summary(candidate.voltage_mv, candidate.target_mhz),
            )

    def fake_sweep_loop(
        _base_curve,
        *,
        settings,
        initial_stable_candidate,
        io,
        unsafe_entries,
        initial_stable_outcome=None,
        **_kwargs,
    ):
        _ = unsafe_entries, initial_stable_outcome
        captured["sweep_calls"].append(
            (
                str(settings.auto_uv_mode),
                int(settings.tail_rise_bins),
                int(initial_stable_candidate.voltage_mv),
                int(initial_stable_candidate.target_mhz),
                bool(settings.descend_through_low_clock),
            )
        )
        assert (
            captured["direct_probe_calls"] == []
        ), "efficiency must not run a private pre-sweep probe stage"
        candidate = VfCurveCandidate(
            label="lower-voltage",
            voltage_mv=950,
            target_mhz=2120,
            flattened_plan=curve,
            metadata={"tail_rise_bins": int(settings.tail_rise_bins)},
        )
        outcome = VoltageProbeOutcome(
            decision=StableRunDecision(
                True,
                FailureKind.NONE,
                FailureSeverity.PASS,
                "stable run",
            ),
            measured_core_clock_mhz=2120.0,
            measured_voltage_mv=950.0,
            raw_probe=_summary(950, 2120),
        )
        io.write_verified_candidate(candidate, outcome)
        return LowerVoltageSweepResult(
            stable_candidate=candidate,
            stable_outcome=outcome,
            state=VoltageSweepState(
                stable_voltage_mv=950,
                stable_target_mhz=2120,
                next_voltage_mv=None,
            ),
        )

    def fake_choice(**kwargs):
        captured["choice_called"] = True
        return (
            kwargs["stable_plan"],
            kwargs["stable_voltage_mv"],
            kwargs["stable_lock_clock_mhz"],
            kwargs["stable_probe"],
            180,
        )

    def fake_final(**kwargs):
        _assert_final_verification_kwargs_match_real_signature(kwargs)
        captured["final_duration_s"] = kwargs["final_verification_duration_s"]
        captured["final_tail_rise_bins"] = kwargs["tail_rise_bins"]
        return "final-result"

    def fake_discovery(*_args, **kwargs):
        captured["discovery_short_s"] = kwargs["short_probe_base_duration_s"]
        return _summary(1000, 2200), SimpleNamespace(success=True)

    monkeypatch.setattr(
        undervolt_main_loop,
        "read_scan_runtime_settings",
        lambda runtime_options, q2rtx_config, gpu_name=None: SimpleNamespace(
            q2rtx_config=q2rtx_config,
            auto_uv_mode="efficiency",
            short_probe_base_duration_s=10,
            configured_min_voltage_mv=None,
            configured_max_drop_pct=15.0,
            min_performance_core_clock_pct=90.0,
            final_verification_duration_s=600,
            final_clock_drop_margin_pct=10.0,
            tail_rise_bins=0,
        ),
    )
    monkeypatch.setattr(undervolt_main_loop, "consume_crash_cache", lambda **_kwargs: [])
    monkeypatch.setattr(undervolt_main_loop, "cleanup_managed_q2rtx_processes", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(undervolt_main_loop, "open_live_gpu_vf_curve_applier", lambda **_kwargs: FakeGpu())
    monkeypatch.setattr(
        undervolt_main_loop,
        "run_discovery_probe",
        fake_discovery,
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "build_loaded_baseline_candidate",
        lambda *_args, **_kwargs: (
            VfCurveCandidate("baseline", 1000, 2200, curve),
            SimpleNamespace(measured_clock_mhz=2200.0),
        ),
    )
    monkeypatch.setattr(undervolt_main_loop, "AutoUvProbeRunner", FakeRunner)
    monkeypatch.setattr(
        undervolt_main_loop,
        "adjust_baseline_to_measured_clock",
        lambda _base_curve, *, candidate, **_kwargs: candidate,
    )
    monkeypatch.setattr(undervolt_main_loop, "write_verified_candidate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(efficiency_uv_loop, "run_base_uv_loop", fake_sweep_loop)
    monkeypatch.setattr(undervolt_main_loop, "choose_final_verification_candidate", fake_choice)
    monkeypatch.setattr(undervolt_main_loop, "run_final_verification_and_save", fake_final)

    result = undervolt_main_loop.run_voltage_frequency_undervolt_main_loop(
        gpu_index=0,
        runtime_options={"auto_uv_require_final_choice": True},
        q2rtx_config=object(),
        log=lambda _message: None,
    )

    assert result == "final-result"
    assert captured["discovery_short_s"] == 10
    assert captured["choice_called"] is True
    assert captured["final_duration_s"] == 180
    assert captured["direct_probe_calls"] == []
    # The tail-tune pass raises the tail by two bins beyond the descent tail and
    # descends through low-clock dips so it can hold the floor clock at lower
    # voltage and push toward the minimum. The first descent does neither.
    assert captured["sweep_calls"] == [
        ("efficiency", 0, 1000, 2200, False),
        ("efficiency-tail-tune", 2, 950, 2120, True),
    ]
    # Final verification keeps the chosen tail-tune candidate's raised tail.
    assert captured["final_tail_rise_bins"] == 2


def test_final_verification_failure_offers_safer_sorted_candidates(
    monkeypatch,
    tmp_path,
) -> None:
    curve = base_curve(875, 1025, 5, 2600, 10)
    captured: dict[str, object] = {"final_calls": []}
    request_path = tmp_path / "final-choice-request.json"
    response_path = tmp_path / "final-choice-response.json"

    class FakeGpu:
        reader = object()
        live_voltage_reader = object()
        runtime_default_plan = curve
        power_limit_w = 320
        clock_ceiling = None
        translated_gpu_policy = {}

        def start_clock_ceiling(self, _target) -> None:
            return None

        def close(self) -> None:
            return None

    class FakeRunner:
        def __init__(self, **_kwargs):
            return None

        def probe_baseline_candidate(self, candidate):
            summary = _summary(candidate.voltage_mv, candidate.target_mhz)
            summary.avg_fps = 65.0
            summary.efficiency_fps_per_w = 65.0 / 300.0
            return VoltageProbeOutcome(
                decision=StableRunDecision(
                    True,
                    FailureKind.NONE,
                    FailureSeverity.PASS,
                    "stable run",
                ),
                measured_core_clock_mhz=float(candidate.target_mhz),
                measured_voltage_mv=float(candidate.voltage_mv),
                raw_probe=summary,
            )

    def probe(voltage_mv: int, clock_mhz: int, fps: float):
        summary = _summary(voltage_mv, clock_mhz)
        summary.avg_fps = float(fps)
        summary.efficiency_fps_per_w = float(fps) / 300.0
        return summary

    def fake_sweep_loop(
        _base_curve,
        *,
        settings,
        io,
        **_kwargs,
    ):
        selected_candidate = None
        selected_outcome = None
        for voltage_mv, clock_mhz, fps in (
            (900, 2940, 68.387),
            (895, 2925, 68.563),
            (890, 2910, 68.472),
            (885, 2895, 68.270),
            (875, 2895, 68.770),
        ):
            candidate = VfCurveCandidate(
                label=f"{voltage_mv}mv",
                voltage_mv=voltage_mv,
                target_mhz=clock_mhz,
                flattened_plan=curve,
                metadata={"tail_rise_bins": int(settings.tail_rise_bins)},
            )
            outcome = VoltageProbeOutcome(
                decision=StableRunDecision(
                    True,
                    FailureKind.NONE,
                    FailureSeverity.PASS,
                    "ok",
                ),
                measured_core_clock_mhz=float(clock_mhz),
                measured_voltage_mv=float(voltage_mv),
                raw_probe=probe(voltage_mv, clock_mhz, fps),
            )
            io.write_verified_candidate(candidate, outcome)
            selected_candidate = candidate
            selected_outcome = outcome
        return LowerVoltageSweepResult(
            stable_candidate=selected_candidate,
            stable_outcome=selected_outcome,
            state=VoltageSweepState(
                stable_voltage_mv=875,
                stable_target_mhz=2895,
                next_voltage_mv=None,
            ),
        )

    def fake_choice(**kwargs):
        return (
            kwargs["stable_plan"],
            kwargs["stable_voltage_mv"],
            kwargs["stable_lock_clock_mhz"],
            kwargs["stable_probe"],
            300,
        )

    def fake_final(**kwargs):
        _assert_final_verification_kwargs_match_real_signature(kwargs)
        captured["final_calls"].append(
            (kwargs["stable_voltage_mv"], kwargs["stable_lock_clock_mhz"])
        )
        if kwargs["stable_voltage_mv"] == 875:
            raise AutoUvError("final long verification failed: fatal-q2rtx-output")
        return "final-result"

    def handle_event(event: str, payload: dict) -> None:
        if event != "final_choice_request":
            return
        captured["retry_request"] = dict(payload)
        response_path.write_text(
            json.dumps(
                {
                    "candidate_id": payload["default_candidate_id"],
                    "final_verification_duration_s": payload[
                        "final_verification_duration_s"
                    ],
                }
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(
        candidate_choice_module,
        "final_choice_request_path",
        lambda: request_path,
    )
    monkeypatch.setattr(
        candidate_choice_module,
        "final_choice_response_path",
        lambda: response_path,
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "read_scan_runtime_settings",
        lambda runtime_options, q2rtx_config, gpu_name=None: SimpleNamespace(
            q2rtx_config=q2rtx_config,
            auto_uv_mode="performance",
            short_probe_base_duration_s=10,
            configured_min_voltage_mv=None,
            configured_max_drop_pct=15.0,
            min_performance_core_clock_pct=90.0,
            final_verification_duration_s=300,
            final_clock_drop_margin_pct=10.0,
            tail_rise_bins=6,
        ),
    )
    monkeypatch.setattr(undervolt_main_loop, "consume_crash_cache", lambda **_kwargs: [])
    monkeypatch.setattr(
        undervolt_main_loop,
        "cleanup_managed_q2rtx_processes",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "open_live_gpu_vf_curve_applier",
        lambda **_kwargs: FakeGpu(),
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "run_discovery_probe",
        lambda *_args, **_kwargs: (_summary(1000, 2753), SimpleNamespace(success=True)),
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "build_loaded_baseline_candidate",
        lambda *_args, **_kwargs: (
            VfCurveCandidate("baseline", 1000, 2753, curve),
            SimpleNamespace(measured_clock_mhz=2753.0),
        ),
    )
    monkeypatch.setattr(undervolt_main_loop, "AutoUvProbeRunner", FakeRunner)
    monkeypatch.setattr(
        undervolt_main_loop,
        "adjust_baseline_to_measured_clock",
        lambda _base_curve, *, candidate, **_kwargs: candidate,
    )
    monkeypatch.setattr(undervolt_main_loop, "write_verified_candidate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(undervolt_main_loop, "run_preset_uv_loop", fake_sweep_loop)
    monkeypatch.setattr(
        undervolt_main_loop,
        "select_performance_auto_oc_candidate",
        lambda _base_curve, **kwargs: (
            kwargs["stable_plan"],
            kwargs["stable_voltage_mv"],
            kwargs["stable_lock_clock_mhz"],
            kwargs["stable_probe"],
            {},
        ),
    )
    monkeypatch.setattr(undervolt_main_loop, "choose_final_verification_candidate", fake_choice)
    monkeypatch.setattr(undervolt_main_loop, "run_final_verification_and_save", fake_final)

    result = undervolt_main_loop.run_voltage_frequency_undervolt_main_loop(
        gpu_index=0,
        runtime_options={"auto_uv_require_final_choice": True},
        q2rtx_config=object(),
        log=lambda _message: None,
        event_callback=handle_event,
    )

    assert result == "final-result"
    assert captured["final_calls"] == [(875, 2895), (895, 2925)]
    retry_request = captured["retry_request"]
    assert retry_request["request_reason"] == "final-verification-failed"
    assert retry_request["default_sort_metric"] == "fps"
    assert retry_request["default_candidate_id"] == "895mv-2925mhz"
    assert [
        candidate["candidate_id"] for candidate in retry_request["candidates"]
    ] == [
        "895mv-2925mhz",
        "890mv-2910mhz",
        "900mv-2940mhz",
        "885mv-2895mhz",
        "1000mv-2753mhz",
    ]
    assert all(
        int(candidate["candidate_voltage_mv"]) > 875
        for candidate in retry_request["candidates"]
    )


def test_performance_auto_oc_runs_before_final_choice(monkeypatch) -> None:
    curve = base_curve(870, 930, 5, 2600, 15)
    captured: dict[str, Any] = {"order": []}

    class FakeGpu:
        reader = object()
        live_voltage_reader = object()
        runtime_default_plan = curve
        power_limit_w = 320
        clock_ceiling = None
        translated_gpu_policy = {"gpu_name": "NVIDIA GeForce RTX 4090"}

        def start_clock_ceiling(self, _target) -> None:
            return None

        def close(self) -> None:
            return None

    class FakeRunner:
        def __init__(self, **_kwargs):
            return None

        def probe_baseline_candidate(self, candidate):
            return VoltageProbeOutcome(
                decision=StableRunDecision(
                    True,
                    FailureKind.NONE,
                    FailureSeverity.PASS,
                    "stable run",
                ),
                measured_core_clock_mhz=float(candidate.target_mhz),
                measured_voltage_mv=float(candidate.voltage_mv),
                raw_probe=_summary(candidate.voltage_mv, candidate.target_mhz),
            )

    def fake_sweep_loop(
        _base_curve,
        *,
        settings,
        initial_stable_candidate,
        io,
        unsafe_entries,
        initial_stable_outcome=None,
        **_kwargs,
    ):
        _ = settings, initial_stable_candidate, unsafe_entries, initial_stable_outcome
        captured["order"].append("lower-sweep")
        candidate = VfCurveCandidate("balanced-result", 870, 2741, curve)
        outcome = VoltageProbeOutcome(
            decision=StableRunDecision(
                True,
                FailureKind.NONE,
                FailureSeverity.PASS,
                "stable run",
            ),
            measured_core_clock_mhz=2741.0,
            measured_voltage_mv=870.0,
            raw_probe=_summary(870, 2741),
        )
        io.write_verified_candidate(candidate, outcome)
        return LowerVoltageSweepResult(
            stable_candidate=candidate,
            stable_outcome=outcome,
            state=VoltageSweepState(
                stable_voltage_mv=870,
                stable_target_mhz=2741,
                next_voltage_mv=None,
            ),
        )

    def fake_auto_oc_search(**kwargs):
        captured["order"].append("auto-oc")
        captured["auto_oc_start"] = (
            kwargs["start_candidate"].voltage_mv,
            kwargs["start_candidate"].target_mhz,
        )
        selected_probe = _summary(910, 2890)
        outcome = VoltageProbeOutcome(
            decision=StableRunDecision(
                True,
                FailureKind.NONE,
                FailureSeverity.PASS,
                "stable run",
            ),
            measured_core_clock_mhz=2890.0,
            measured_voltage_mv=910.0,
            raw_probe=selected_probe,
        )
        return SimpleNamespace(
            selected_candidate=VfCurveCandidate("performance-oc", 910, 2890, curve),
            selected_probe=selected_probe,
            attempts=[SimpleNamespace(outcome=outcome)],
        )

    def fake_choice(**kwargs):
        captured["order"].append("choice")
        captured["choice_stable"] = (
            kwargs["stable_voltage_mv"],
            kwargs["stable_lock_clock_mhz"],
        )
        captured["choice_history"] = [
            (int(probe.candidate_voltage_mv), int(probe.lock_clock_mhz))
            for probe in kwargs["stable_history"]
        ]
        return (
            kwargs["stable_plan"],
            kwargs["stable_voltage_mv"],
            kwargs["stable_lock_clock_mhz"],
            kwargs["stable_probe"],
            600,
        )

    def fake_final(**kwargs):
        _assert_final_verification_kwargs_match_real_signature(kwargs)
        captured["order"].append("final")
        captured["final"] = (
            kwargs["stable_voltage_mv"],
            kwargs["stable_lock_clock_mhz"],
        )
        return "final-result"

    monkeypatch.setattr(
        undervolt_main_loop,
        "read_scan_runtime_settings",
        lambda runtime_options, q2rtx_config, gpu_name=None: SimpleNamespace(
            q2rtx_config=q2rtx_config,
            auto_uv_mode="performance",
            short_probe_base_duration_s=10,
            configured_min_voltage_mv=None,
            configured_max_drop_pct=15.0,
            min_performance_core_clock_pct=90.0,
            final_verification_duration_s=600,
            final_clock_drop_margin_pct=10.0,
            tail_rise_bins=6,
        ),
    )
    monkeypatch.setattr(undervolt_main_loop, "consume_crash_cache", lambda **_kwargs: [])
    monkeypatch.setattr(
        undervolt_main_loop,
        "cleanup_managed_q2rtx_processes",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "open_live_gpu_vf_curve_applier",
        lambda **_kwargs: FakeGpu(),
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "run_discovery_probe",
        lambda *_args, **_kwargs: (_summary(1000, 2745), SimpleNamespace(success=True)),
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "build_loaded_baseline_candidate",
        lambda *_args, **_kwargs: (
            VfCurveCandidate("baseline", 1000, 2745, curve),
            SimpleNamespace(measured_clock_mhz=2745.0),
        ),
    )
    monkeypatch.setattr(undervolt_main_loop, "AutoUvProbeRunner", FakeRunner)
    monkeypatch.setattr(
        undervolt_main_loop,
        "adjust_baseline_to_measured_clock",
        lambda _base_curve, *, candidate, **_kwargs: candidate,
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "write_verified_candidate",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(undervolt_main_loop, "run_preset_uv_loop", fake_sweep_loop)
    monkeypatch.setattr(
        performance_uv_loop,
        "run_auto_oc_candidate_search",
        fake_auto_oc_search,
    )
    monkeypatch.setattr(undervolt_main_loop, "choose_final_verification_candidate", fake_choice)
    monkeypatch.setattr(undervolt_main_loop, "run_final_verification_and_save", fake_final)

    result = undervolt_main_loop.run_voltage_frequency_undervolt_main_loop(
        gpu_index=0,
        runtime_options={"auto_uv_require_final_choice": True},
        q2rtx_config=object(),
        log=lambda _message: None,
    )

    assert result == "final-result"
    assert captured["order"] == ["lower-sweep", "auto-oc", "choice", "final"]
    assert captured["auto_oc_start"] == (870, 2741)
    assert captured["choice_stable"] == (910, 2890)
    assert (910, 2890) in captured["choice_history"]
    assert captured["final"] == (910, 2890)


def test_previous_crash_resume_starts_auto_oc_from_next_saved_voltage(
    monkeypatch,
) -> None:
    curve = base_curve(870, 930, 5, 2600, 15)
    recovery_records = [
        {
            "candidate_id": "old-885mv-2895mhz",
            "candidate_voltage_mv": 885,
            "lock_clock_mhz": 2895,
            "avg_fps": 260.0,
            "plan": curve,
            "tail_rise_bins": 6,
        },
        {
            "candidate_id": "old-875mv-2880mhz",
            "candidate_voltage_mv": 875,
            "lock_clock_mhz": 2880,
            "avg_fps": 255.0,
            "plan": curve,
            "tail_rise_bins": 6,
        },
        {
            "candidate_id": "900mv-2786mhz",
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2786,
            "avg_fps": 190.0,
            "plan": curve,
            "tail_rise_bins": 6,
        },
        {
            "candidate_id": "890mv-2855mhz",
            "candidate_voltage_mv": 890,
            "lock_clock_mhz": 2855,
            "avg_fps": 197.0,
            "plan": curve,
            "tail_rise_bins": 6,
        },
        {
            "candidate_id": "885mv-2873mhz",
            "candidate_voltage_mv": 885,
            "lock_clock_mhz": 2873,
            "avg_fps": 198.0,
            "avg_core_clock_mhz": 2868.0,
            "avg_power_w": 220.0,
            "efficiency_fps_per_w": 0.9,
            "base_candidate_voltage_mv": 1000,
            "base_lock_clock_mhz": 2700,
            "base_avg_core_clock_mhz": 2700.0,
            "base_avg_fps": 180.0,
            "base_avg_power_w": 300.0,
            "base_efficiency_fps_per_w": 0.6,
            "plan": curve,
            "tail_rise_bins": 6,
        },
        {
            "candidate_id": "875mv-2897mhz",
            "candidate_voltage_mv": 875,
            "lock_clock_mhz": 2897,
            "avg_fps": 205.0,
            "plan": curve,
            "tail_rise_bins": 6,
        },
    ]
    unsafe_entry = {
        "recorded_at": "2026-06-20T17:20:09+02:00",
        "reason": "stability-probe-failed",
        "candidate_voltage_mv": 875,
        "lock_clock_mhz": 2910,
        "details": {"result_reason": "fatal-q2rtx-output"},
    }
    captured: dict[str, object] = {"order": []}
    events: list[tuple[str, dict]] = []

    class FakeGpu:
        reader = object()
        live_voltage_reader = object()
        runtime_default_plan = curve
        power_limit_w = 320
        clock_ceiling = None
        translated_gpu_policy = {"gpu_name": "NVIDIA GeForce RTX 4090"}

        def start_clock_ceiling(self, _target) -> None:
            return None

        def close(self) -> None:
            return None

    class FakeRunner:
        def __init__(self, **_kwargs):
            return None

        def probe_baseline_candidate(self, candidate):
            return VoltageProbeOutcome(
                decision=StableRunDecision(
                    True,
                    FailureKind.NONE,
                    FailureSeverity.PASS,
                    "stable run",
                ),
                measured_core_clock_mhz=float(candidate.target_mhz),
                measured_voltage_mv=float(candidate.voltage_mv),
                raw_probe=_summary(candidate.voltage_mv, candidate.target_mhz),
            )

    def fail_sweep(*_args, **_kwargs):
        raise AssertionError("resume should skip the completed lower-voltage sweep")

    def fake_discovery(*_args, **_kwargs):
        raise AssertionError("resume should reuse the saved baseline probe")

    def fake_recovery_choice(**kwargs):
        captured["order"].append("recovery-choice")
        captured["recovery_default"] = kwargs["default_candidate_id"]
        captured["recovery_decision"] = dict(kwargs["recovery_decision"])
        captured["recovery_base_probe"] = kwargs["base_probe"]
        captured["candidate_ids"] = [
            record["candidate_id"] for record in kwargs["candidate_records"]
        ]
        selected = next(
            record
            for record in kwargs["candidate_records"]
            if record["candidate_id"] == "885mv-2873mhz"
        )
        return (
            selected["plan"],
            selected["candidate_voltage_mv"],
            selected["lock_clock_mhz"],
            None,
            kwargs["final_verification_duration_s"],
            selected["tail_rise_bins"],
            selected,
        )

    def fake_auto_oc(_base_curve, **kwargs):
        captured["order"].append("auto-oc")
        captured["auto_oc_start"] = (
            kwargs["stable_voltage_mv"],
            kwargs["stable_lock_clock_mhz"],
        )
        captured["auto_oc_probe"] = kwargs["stable_probe"]
        captured["auto_oc_baseline"] = kwargs["measured_baseline_clock_mhz"]
        return (
            kwargs["stable_plan"],
            kwargs["stable_voltage_mv"],
            kwargs["stable_lock_clock_mhz"],
            kwargs["stable_probe"],
            {},
        )

    def fake_choice(**kwargs):
        captured["order"].append("final-choice")
        captured["choice_stable"] = (
            kwargs["stable_voltage_mv"],
            kwargs["stable_lock_clock_mhz"],
        )
        captured["choice_history"] = [
            (int(probe.candidate_voltage_mv), int(probe.lock_clock_mhz))
            for probe in kwargs["stable_history"]
        ]
        return (
            kwargs["stable_plan"],
            kwargs["stable_voltage_mv"],
            kwargs["stable_lock_clock_mhz"],
            kwargs["stable_probe"],
            600,
        )

    def fake_final(**kwargs):
        _assert_final_verification_kwargs_match_real_signature(kwargs)
        captured["order"].append("final")
        captured["final"] = (
            kwargs["stable_voltage_mv"],
            kwargs["stable_lock_clock_mhz"],
        )
        return "final-result"

    monkeypatch.setattr(
        undervolt_main_loop,
        "read_scan_runtime_settings",
        lambda runtime_options, q2rtx_config, gpu_name=None: SimpleNamespace(
            q2rtx_config=q2rtx_config,
            auto_uv_mode="performance",
            short_probe_base_duration_s=10,
            configured_min_voltage_mv=None,
            configured_max_drop_pct=15.0,
            min_performance_core_clock_pct=90.0,
            final_verification_duration_s=600,
            final_clock_drop_margin_pct=10.0,
            tail_rise_bins=6,
        ),
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "consume_crash_cache",
        lambda **_kwargs: crash_recovery.CrashCacheEntries(
            [unsafe_entry],
            interrupted_entry=unsafe_entry,
        ),
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "cleanup_managed_q2rtx_processes",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "open_live_gpu_vf_curve_applier",
        lambda **_kwargs: FakeGpu(),
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "run_discovery_probe",
        fake_discovery,
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "build_loaded_baseline_candidate",
        lambda *_args, **_kwargs: (
            VfCurveCandidate("baseline", 1000, 2700, curve),
            SimpleNamespace(measured_clock_mhz=2700.0),
        ),
    )
    monkeypatch.setattr(undervolt_main_loop, "AutoUvProbeRunner", FakeRunner)
    monkeypatch.setattr(
        undervolt_main_loop,
        "adjust_baseline_to_measured_clock",
        lambda _base_curve, *, candidate, **_kwargs: candidate,
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "write_verified_candidate",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(undervolt_main_loop, "read_verified_candidates", lambda: recovery_records)
    monkeypatch.setattr(undervolt_main_loop, "run_preset_uv_loop", fail_sweep)
    monkeypatch.setattr(
        undervolt_main_loop,
        "choose_recovery_final_verification_candidate",
        fake_recovery_choice,
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "select_performance_auto_oc_candidate",
        fake_auto_oc,
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "choose_final_verification_candidate",
        fake_choice,
    )
    monkeypatch.setattr(undervolt_main_loop, "run_final_verification_and_save", fake_final)

    result = undervolt_main_loop.run_voltage_frequency_undervolt_main_loop(
        gpu_index=0,
        runtime_options={"auto_uv_require_final_choice": True},
        q2rtx_config=object(),
        log=lambda _message: None,
        event_callback=lambda name, payload: events.append((name, dict(payload))),
    )

    assert result == "final-result"
    assert captured["order"] == [
        "recovery-choice",
        "auto-oc",
        "final-choice",
        "final",
    ]
    assert captured["recovery_default"] == "885mv-2873mhz"
    assert captured["recovery_base_probe"] is None
    assert captured["recovery_decision"]["candidate_voltage_mv"] == 875
    assert captured["candidate_ids"] == [
        "875mv-2897mhz",
        "885mv-2873mhz",
        "890mv-2855mhz",
        "900mv-2786mhz",
    ]
    assert captured["auto_oc_start"] == (885, 2873)
    assert captured["auto_oc_baseline"] == 2700.0
    assert captured["auto_oc_probe"] is not None
    assert captured["choice_stable"] == (885, 2873)
    assert (885, 2873) in captured["choice_history"]
    assert captured["final"] == (885, 2873)
    replayed_probe_results = [
        payload for name, payload in events if name == "probe_result"
    ]
    assert [payload["stage"] for payload in replayed_probe_results[:2]] == [
        "base-baseline",
        "candidate",
    ]
    assert replayed_probe_results[0]["fps"] == 180.0
    assert replayed_probe_results[0]["power_w"] == 300.0
    assert replayed_probe_results[0]["efficiency_fps_per_w"] == 0.6
    assert replayed_probe_results[1]["fps"] == 198.0
    assert replayed_probe_results[1]["power_w"] == 220.0
    assert replayed_probe_results[1]["efficiency_fps_per_w"] == 0.9


def test_auto_uv_user_stop_offers_stable_history_for_final_choice(monkeypatch) -> None:
    curve = base_curve(900, 1025, 25, 2000, 40)
    captured: dict[str, object] = {}

    class FakeGpu:
        reader = object()
        live_voltage_reader = object()
        runtime_default_plan = curve
        power_limit_w = 320
        clock_ceiling = None
        translated_gpu_policy = {}

        def start_clock_ceiling(self, _target) -> None:
            return None

        def close(self) -> None:
            return None

    class FakeRunner:
        def __init__(self, **_kwargs):
            return None

        def probe_baseline_candidate(self, candidate):
            return VoltageProbeOutcome(
                decision=StableRunDecision(
                    True,
                    FailureKind.NONE,
                    FailureSeverity.PASS,
                    "stable run",
                ),
                measured_core_clock_mhz=float(candidate.target_mhz),
                measured_voltage_mv=float(candidate.voltage_mv),
                raw_probe=_summary(candidate.voltage_mv, candidate.target_mhz),
            )

    def fake_sweep_loop(*_args, **_kwargs):
        raise KeyboardInterrupt()

    def fake_choice(**kwargs):
        captured["choice_called"] = True
        captured["request_reason"] = kwargs["request_reason"]
        captured["history"] = [
            (
                int(probe.candidate_voltage_mv),
                int(probe.lock_clock_mhz),
            )
            for probe in kwargs["stable_history"]
        ]
        return (
            kwargs["stable_plan"],
            kwargs["stable_voltage_mv"],
            kwargs["stable_lock_clock_mhz"],
            kwargs["stable_probe"],
            240,
        )

    def fake_final(**kwargs):
        _assert_final_verification_kwargs_match_real_signature(kwargs)
        captured["final_duration_s"] = kwargs["final_verification_duration_s"]
        return "final-result"

    monkeypatch.setattr(
        undervolt_main_loop,
        "read_scan_runtime_settings",
        lambda runtime_options, q2rtx_config, gpu_name=None: SimpleNamespace(
            q2rtx_config=q2rtx_config,
            auto_uv_mode="performance",
            short_probe_base_duration_s=10,
            configured_min_voltage_mv=None,
            configured_max_drop_pct=15.0,
            min_performance_core_clock_pct=90.0,
            final_verification_duration_s=600,
            final_clock_drop_margin_pct=10.0,
            tail_rise_bins=6,
        ),
    )
    monkeypatch.setattr(undervolt_main_loop, "consume_crash_cache", lambda **_kwargs: [])
    monkeypatch.setattr(
        undervolt_main_loop,
        "cleanup_managed_q2rtx_processes",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "open_live_gpu_vf_curve_applier",
        lambda **_kwargs: FakeGpu(),
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "run_discovery_probe",
        lambda *_args, **_kwargs: (_summary(1000, 2200), SimpleNamespace(success=True)),
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "build_loaded_baseline_candidate",
        lambda *_args, **_kwargs: (
            VfCurveCandidate("baseline", 1000, 2200, curve),
            SimpleNamespace(measured_clock_mhz=2200.0),
        ),
    )
    monkeypatch.setattr(undervolt_main_loop, "AutoUvProbeRunner", FakeRunner)
    monkeypatch.setattr(
        undervolt_main_loop,
        "adjust_baseline_to_measured_clock",
        lambda _base_curve, *, candidate, **_kwargs: candidate,
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "write_verified_candidate",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(undervolt_main_loop, "run_preset_uv_loop", fake_sweep_loop)
    monkeypatch.setattr(undervolt_main_loop, "choose_final_verification_candidate", fake_choice)
    monkeypatch.setattr(undervolt_main_loop, "run_final_verification_and_save", fake_final)
    monkeypatch.setattr(
        undervolt_main_loop,
        "clear_auto_uv_stop_request",
        lambda: captured.setdefault("stop_request_cleared", True),
    )

    result = undervolt_main_loop.run_voltage_frequency_undervolt_main_loop(
        gpu_index=0,
        runtime_options={"auto_uv_require_final_choice": True},
        q2rtx_config=object(),
        log=lambda _message: None,
    )

    assert result == "final-result"
    assert captured["choice_called"] is True
    assert captured["request_reason"] == "user-stop"
    assert captured["stop_request_cleared"] is True
    assert captured["history"] == [(1000, 2200)]
    assert captured["final_duration_s"] == 240


def test_performance_uv_loop_runs_before_final_verification(monkeypatch) -> None:
    curve = base_curve(900, 1000, 25, 2000, 40)
    start_probe = _summary(925, 2600)
    captured = {}

    def fake_auto_oc_search(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            selected_candidate=VfCurveCandidate(
                "auto-oc-selected",
                950,
                2745,
                curve,
            ),
        )

    monkeypatch.setattr(
        performance_uv_loop,
        "run_auto_oc_candidate_search",
        fake_auto_oc_search,
    )

    plan, voltage_mv, clock_mhz, probe, _oc_metadata = (
        performance_uv_loop.select_performance_auto_oc_candidate(
            curve,
            auto_uv_mode="performance",
            stable_plan=curve,
            stable_voltage_mv=925,
            stable_lock_clock_mhz=2600,
            stable_probe=start_probe,
            stable_history=[],
            runner=object(),
            gpu_name="NVIDIA GeForce RTX 4090",
            clock_ceiling=None,
            probe_history=[],
            log=lambda _message: None,
            tail_rise_bins=6,
            target_voltage_mv=940,
            target_clock_mhz=2700,
        )
    )

    assert plan is curve
    assert voltage_mv == 950
    assert clock_mhz == 2745
    assert probe is None
    assert captured["start_candidate"].voltage_mv == 925
    assert captured["start_candidate"].target_mhz == 2600
    assert captured["tail_rise_bins"] == 6
    assert captured["target_voltage_mv"] == 940
    assert captured["target_clock_mhz"] == 2700


def test_non_performance_mode_skips_auto_oc_selection(monkeypatch) -> None:
    curve = base_curve(900, 1000, 25, 2000, 40)
    start_probe = _summary(925, 2600)

    def fail_auto_oc_search(**_kwargs):
        raise AssertionError("auto-oc should not run outside performance mode")

    monkeypatch.setattr(
        performance_uv_loop,
        "run_auto_oc_candidate_search",
        fail_auto_oc_search,
    )

    plan, voltage_mv, clock_mhz, probe, _oc_metadata = (
        performance_uv_loop.select_performance_auto_oc_candidate(
            curve,
            auto_uv_mode="efficiency",
            stable_plan=curve,
            stable_voltage_mv=925,
            stable_lock_clock_mhz=2600,
            stable_probe=start_probe,
            stable_history=[],
            runner=object(),
            gpu_name="NVIDIA GeForce RTX 4090",
            clock_ceiling=None,
            probe_history=[],
            log=lambda _message: None,
        )
    )

    assert plan is curve
    assert voltage_mv == 925
    assert clock_mhz == 2600
    assert probe is start_probe


# --- positive_int ---------------------------------------------------------


def test_positive_int_parses_positive_values() -> None:
    assert undervolt_main_loop.positive_int(940) == 940
    assert undervolt_main_loop.positive_int("2700") == 2700


def test_positive_int_rejects_non_positive_and_unparseable() -> None:
    assert undervolt_main_loop.positive_int(0) is None
    assert undervolt_main_loop.positive_int(-5) is None
    assert undervolt_main_loop.positive_int(None) is None
    assert undervolt_main_loop.positive_int("not-a-number") is None


# --- _format_optional_pct -------------------------------------------------


def test_format_optional_pct_handles_none_and_value() -> None:
    assert undervolt_main_loop._format_optional_pct(None) == "n/a"
    assert undervolt_main_loop._format_optional_pct(12.345) == "12.35%"


# --- require_probe_summary ------------------------------------------------


def test_require_probe_summary_returns_raw_probe() -> None:
    probe = _summary(950, 2100)
    outcome = VoltageProbeOutcome(
        decision=StableRunDecision(
            True, FailureKind.NONE, FailureSeverity.PASS, "stable run"
        ),
        measured_core_clock_mhz=2100.0,
        measured_voltage_mv=950.0,
        raw_probe=probe,
    )
    assert undervolt_main_loop.require_probe_summary(outcome) is probe


def test_require_probe_summary_raises_without_probe() -> None:
    outcome = VoltageProbeOutcome(
        decision=StableRunDecision(
            True, FailureKind.NONE, FailureSeverity.PASS, "stable run"
        ),
        measured_core_clock_mhz=2100.0,
        measured_voltage_mv=950.0,
        raw_probe=None,
    )
    with pytest.raises(AutoUvError):
        undervolt_main_loop.require_probe_summary(outcome)


# --- run_auto_oc_candidate_search lazy import -----------------------------


def test_run_auto_oc_candidate_search_delegates_to_auto_oc(monkeypatch) -> None:
    import auto_uv.auto_oc.search as auto_oc_search

    captured: dict[str, object] = {}

    def fake_search(**kwargs):
        captured.update(kwargs)
        return "auto-oc-result"

    monkeypatch.setattr(auto_oc_search, "run_auto_oc_candidate_search", fake_search)

    result = performance_uv_loop.run_auto_oc_candidate_search(foo=1, bar="baz")

    assert result == "auto-oc-result"
    assert captured == {"foo": 1, "bar": "baz"}


# --- consume_crash_cache --------------------------------------------------


def test_consume_crash_cache_without_interrupted_marker(monkeypatch) -> None:
    monkeypatch.setattr(
        crash_recovery,
        "consume_interrupted_probe_crash_marker",
        lambda: None,
    )
    monkeypatch.setattr(
        crash_recovery,
        "load_unsafe_voltage_blacklist",
        lambda: ["blacklist-entry"],
    )
    log_messages: list[str] = []

    entries = crash_recovery.consume_crash_cache(log=log_messages.append)

    assert entries == ["blacklist-entry"]
    assert log_messages == []


def test_consume_crash_cache_logs_interrupted_marker(monkeypatch) -> None:
    monkeypatch.setattr(
        crash_recovery,
        "consume_interrupted_probe_crash_marker",
        lambda: (
            "/tmp/crash.json",
            {"candidate_voltage_mv": 880, "lock_clock_mhz": 2600},
        ),
    )
    monkeypatch.setattr(
        crash_recovery,
        "load_unsafe_voltage_blacklist",
        lambda: ["entry"],
    )
    log_messages: list[str] = []

    entries = crash_recovery.consume_crash_cache(log=log_messages.append)

    assert entries == ["entry"]
    assert any("blacklisted=880mV" in message for message in log_messages)
    assert any("target=2600MHz" in message for message in log_messages)


def test_next_safer_recovery_candidate_uses_one_voltage_bin_up() -> None:
    records = [
        {
            "candidate_id": "875mv-2897mhz",
            "candidate_voltage_mv": 875,
            "lock_clock_mhz": 2897,
            "avg_fps": 210.0,
            "plan": [{"voltage_mv": 875}],
        },
        {
            "candidate_id": "885mv-2873mhz",
            "candidate_voltage_mv": 885,
            "lock_clock_mhz": 2873,
            "avg_fps": 190.0,
            "plan": [{"voltage_mv": 885}],
        },
        {
            "candidate_id": "885mv-2880mhz",
            "candidate_voltage_mv": 885,
            "lock_clock_mhz": 2880,
            "avg_fps": 195.0,
            "plan": [{"voltage_mv": 885}],
        },
        {
            "candidate_id": "900mv-2786mhz",
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2786,
            "avg_fps": 220.0,
            "plan": [{"voltage_mv": 900}],
        },
    ]

    assert (
        undervolt_main_loop.next_safer_recovery_candidate_id(
            records,
            failed_voltage_mv=875,
            auto_uv_mode="performance",
        )
        == "885mv-2880mhz"
    )


def test_recovery_candidate_records_use_last_failed_scan_block() -> None:
    curve = [{"voltage_mv": 900}]
    records = [
        {
            "candidate_id": "old-885mv-2895mhz",
            "candidate_voltage_mv": 885,
            "lock_clock_mhz": 2895,
            "avg_fps": 260.0,
            "plan": curve,
        },
        {
            "candidate_id": "old-875mv-2880mhz",
            "candidate_voltage_mv": 875,
            "lock_clock_mhz": 2880,
            "avg_fps": 255.0,
            "plan": curve,
        },
        {
            "candidate_id": "900mv-2786mhz",
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2786,
            "avg_fps": 190.0,
            "plan": curve,
        },
        {
            "candidate_id": "885mv-2873mhz",
            "candidate_voltage_mv": 885,
            "lock_clock_mhz": 2873,
            "avg_fps": 198.0,
            "plan": curve,
        },
        {
            "candidate_id": "875mv-2897mhz",
            "candidate_voltage_mv": 875,
            "lock_clock_mhz": 2897,
            "avg_fps": 205.0,
            "plan": curve,
        },
    ]

    crashed_run = undervolt_main_loop.recovery_candidate_records_for_failed_run(
        records,
        crash_recovery_entry={"candidate_voltage_mv": 875},
    )

    assert [record["candidate_id"] for record in crashed_run] == [
        "875mv-2897mhz",
        "885mv-2873mhz",
        "900mv-2786mhz",
    ]
    assert (
        undervolt_main_loop.next_safer_recovery_candidate_id(
            crashed_run,
            failed_voltage_mv=875,
            auto_uv_mode="performance",
        )
        == "885mv-2873mhz"
    )


def test_final_choice_request_recovery_records_rebuild_plans(
    monkeypatch,
    tmp_path,
) -> None:
    request_path = tmp_path / "auto-uv-final-choice-request.json"
    request_path.write_text(
        json.dumps(
            {
                "auto_uv_mode": "performance",
                "candidates": [
                    {
                        "candidate_id": "875mv-2895mhz",
                        "candidate_voltage_mv": 875,
                        "lock_clock_mhz": 2895,
                        "avg_fps": 68.77,
                    },
                    {
                        "candidate_id": "895mv-2925mhz",
                        "candidate_voltage_mv": 895,
                        "lock_clock_mhz": 2925,
                        "avg_fps": 68.563,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        crash_recovery,
        "final_choice_request_path",
        lambda: request_path,
    )

    records = crash_recovery.final_choice_request_recovery_records(
        base_curve(875, 1025, 5, 2600, 10),
        fallback_records=[],
        failed_voltage_mv=875,
        failed_lock_clock_mhz=2895,
        auto_uv_mode="performance",
        tail_rise_bins=6,
    )

    assert [record["candidate_id"] for record in records] == [
        "875mv-2895mhz",
        "895mv-2925mhz",
    ]
    assert records[1]["tail_rise_bins"] == 6
    assert records[1]["plan"]


def test_crash_recovery_uses_only_interrupted_marker_entry() -> None:
    old_unsafe_entry = {
        "candidate_voltage_mv": 875,
        "lock_clock_mhz": 2910,
        "reason": "stability-probe-failed",
    }
    entries = crash_recovery.CrashCacheEntries(
        [old_unsafe_entry],
        interrupted_entry=None,
    )

    assert crash_recovery.crash_recovery_entry_from_cache(entries) is None


def test_crash_recovery_entry_profile_tier_reads_nested_marker_details() -> None:
    entry = {
        "candidate_voltage_mv": 875,
        "details": {
            "marker_details": {
                "generated_profile_tier": "performance",
                "tail_rise_bins": 6,
            }
        },
    }

    assert crash_recovery.crash_recovery_entry_profile_tier(entry) == "performance"


def test_recovery_candidate_records_filter_tier_before_last_run_block() -> None:
    curve = [{"voltage_mv": 900}]
    records = [
        {
            "candidate_id": "eff-900",
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2775,
            "plan": curve,
            "tail_rise_bins": 0,
        },
        {
            "candidate_id": "eff-885",
            "candidate_voltage_mv": 885,
            "lock_clock_mhz": 2760,
            "plan": curve,
            "tail_rise_bins": 0,
        },
        {
            "candidate_id": "bal-900",
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2820,
            "plan": curve,
            "tail_rise_bins": 4,
        },
        {
            "candidate_id": "bal-885",
            "candidate_voltage_mv": 885,
            "lock_clock_mhz": 2835,
            "plan": curve,
            "tail_rise_bins": 4,
        },
    ]

    efficiency_run = undervolt_main_loop.recovery_candidate_records_for_failed_run(
        records,
        crash_recovery_entry={
            "candidate_voltage_mv": 875,
            "details": {"generated_profile_tier": "efficiency"},
        },
        target_profile_tier="efficiency",
    )
    balanced_run = undervolt_main_loop.recovery_candidate_records_for_failed_run(
        records,
        crash_recovery_entry={
            "candidate_voltage_mv": 875,
            "details": {"generated_profile_tier": "efficiency"},
        },
        target_profile_tier="balanced",
    )

    assert [record["candidate_id"] for record in efficiency_run] == [
        "eff-885",
        "eff-900",
    ]
    assert balanced_run == []


def test_auto_uv_run_profile_tier_uses_tail_bins_for_balanced_mode() -> None:
    settings = SimpleNamespace(auto_uv_mode="efficiency")

    assert (
        undervolt_main_loop.auto_uv_run_profile_tier(
            {},
            settings,
            tail_rise_bins=4,
        )
        == "balanced"
    )
    assert (
        undervolt_main_loop.auto_uv_run_profile_tier(
            {},
            settings,
            tail_rise_bins=0,
        )
        == "efficiency"
    )


def test_auto_uv_run_profile_tier_is_tier_agnostic_for_adaptive_runs() -> None:
    # Adaptive runs sweep all three tiers; falling into tail-bins inference
    # would mislabel their crash markers as efficiency history.
    settings = SimpleNamespace(auto_uv_mode="adaptive")

    assert (
        undervolt_main_loop.auto_uv_run_profile_tier(
            {"auto_uv_requested_mode": "adaptive", "auto_uv_mode": "adaptive"},
            settings,
            tail_rise_bins=0,
        )
        == ""
    )
    assert (
        undervolt_main_loop.auto_uv_run_profile_tier(
            {},
            settings,
            tail_rise_bins=0,
        )
        == ""
    )


def test_recovery_records_after_adaptive_crash_offer_any_tier() -> None:
    records = [
        {
            "candidate_id": "perf-905",
            "candidate_voltage_mv": 905,
            "lock_clock_mhz": 2910,
            "generated_profile_tier": "performance",
            "plan": [{"voltage_mv": 905, "new_offset_mhz": 0}],
        },
    ]

    performance_run = undervolt_main_loop.recovery_candidate_records_for_failed_run(
        records,
        crash_recovery_entry={
            "candidate_voltage_mv": 875,
            "details": {
                "marker_details": {"auto_uv_mode": "adaptive", "tail_rise_bins": 0}
            },
        },
        target_profile_tier="performance",
    )

    assert [record["candidate_id"] for record in performance_run] == ["perf-905"]


def test_crash_recovery_decision_reads_previous_device_lost_log(tmp_path) -> None:
    log_path = tmp_path / "q2rtx.log"
    log_path.write_text(
        "ordinary line\n[2026-06-20] Device lost!\n",
        encoding="utf-8",
    )

    decision = undervolt_main_loop.crash_recovery_decision(
        {
            "recorded_at": "2026-06-20T17:20:09+02:00",
            "reason": "stability-probe-failed",
            "candidate_voltage_mv": 875,
            "lock_clock_mhz": 2910,
            "phase": "candidate",
            "details": {
                "result_reason": "fatal-q2rtx-output",
                "log_path": str(log_path),
            },
        }
    )

    assert decision["candidate_voltage_mv"] == 875
    assert decision["lock_clock_mhz"] == 2910
    assert "Device lost!" in decision["decision"]
    assert decision["result_reason"] == "fatal-q2rtx-output"


# --- retarget_clock_ceiling_for_candidate ---------------------------------


def test_retarget_clock_ceiling_noop_when_ceiling_is_none() -> None:
    candidate = VfCurveCandidate("c", 950, 2120, base_curve(900, 1025, 25, 2000, 40))
    # Must not raise when there is no live clock ceiling.
    undervolt_main_loop.retarget_clock_ceiling_for_candidate(None, candidate)


def test_retarget_clock_ceiling_forwards_tail_ceiling() -> None:
    curve = base_curve(900, 1025, 25, 2000, 40)
    candidate = VfCurveCandidate("c", 950, 2120, curve)
    captured: dict[str, object] = {}

    class FakeCeiling:
        def retarget(self, *, lock_clock_mhz, lock_voltage_mv, ceiling_clock_mhz):
            captured["lock_clock_mhz"] = lock_clock_mhz
            captured["lock_voltage_mv"] = lock_voltage_mv
            captured["ceiling_clock_mhz"] = ceiling_clock_mhz

    undervolt_main_loop.retarget_clock_ceiling_for_candidate(FakeCeiling(), candidate)

    assert captured["lock_clock_mhz"] == 2120
    assert captured["lock_voltage_mv"] == 950
    # The ceiling is derived from the candidate's plan, never below the lock.
    assert captured["ceiling_clock_mhz"] >= 2120


# --- write_verified_candidate ---------------------------------------------


def test_write_verified_candidate_prefers_metadata_tail_rise_bins(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        baseline_probe,
        "write_latest_verified_candidate",
        lambda **kwargs: captured.update(kwargs),
    )
    curve = base_curve(900, 1025, 25, 2000, 40)
    candidate = VfCurveCandidate(
        "c", 950, 2120, curve, metadata={"tail_rise_bins": 3}
    )

    baseline_probe.write_verified_candidate(
        candidate,
        _summary(950, 2120),
        discovery_summary=_summary(1000, 2200),
        tail_rise_bins=7,
    )

    assert captured["lock_clock_mhz"] == 2120
    assert captured["voltage_mv"] == 950
    # Metadata tail-rise bins win over the passed-in default.
    assert captured["tail_rise_bins"] == 3


def test_write_verified_candidate_falls_back_to_argument_tail_rise_bins(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        baseline_probe,
        "write_latest_verified_candidate",
        lambda **kwargs: captured.update(kwargs),
    )
    curve = base_curve(900, 1025, 25, 2000, 40)
    candidate = VfCurveCandidate("c", 950, 2120, curve)

    baseline_probe.write_verified_candidate(
        candidate,
        _summary(950, 2120),
        discovery_summary=_summary(1000, 2200),
        tail_rise_bins=7,
    )

    assert captured["tail_rise_bins"] == 7


# --- build_loaded_baseline_candidate (pure curve logic) -------------------


def _loaded_discovery_result():
    telemetry = [
        {"elapsed_s": 6.0, "power_w": 300.0, "gpu_util_pct": 99.0,
         "core_clock_mhz": 2100.0, "voltage_mv": 950.0},
        {"elapsed_s": 7.0, "power_w": 305.0, "gpu_util_pct": 99.0,
         "core_clock_mhz": 2110.0, "voltage_mv": 955.0},
        {"elapsed_s": 8.0, "power_w": 310.0, "gpu_util_pct": 99.0,
         "core_clock_mhz": 2120.0, "voltage_mv": 960.0},
    ]
    return SimpleNamespace(telemetry_samples=telemetry)


def test_build_loaded_baseline_candidate_uses_loaded_band_and_target() -> None:
    curve = base_curve(900, 1025, 25, 2000, 40)
    candidate, target = undervolt_main_loop.build_loaded_baseline_candidate(
        curve,
        discovery_summary=SimpleNamespace(avg_core_clock_mhz=2100.0),
        discovery_result=_loaded_discovery_result(),
        power_limit_w=320,
        tail_rise_bins=2,
    )

    assert candidate.label == "baseline-loaded-flattened-curve"
    # Voltage snaps to an editable bin and the target comes from the load model.
    assert candidate.voltage_mv in {p["voltage_mv"] for p in curve}
    assert candidate.target_mhz == int(target.target_clock_mhz)
    assert candidate.flattened_plan, "a flattened plan must be produced"


def test_build_loaded_baseline_candidate_empty_telemetry_uses_fallbacks() -> None:
    curve = base_curve(900, 1025, 25, 2000, 40)
    candidate, target = undervolt_main_loop.build_loaded_baseline_candidate(
        curve,
        discovery_summary=SimpleNamespace(avg_core_clock_mhz=2080.0),
        discovery_result=SimpleNamespace(telemetry_samples=[]),
        power_limit_w=None,
        tail_rise_bins=0,
    )

    assert candidate.target_mhz == int(target.target_clock_mhz)
    assert candidate.voltage_mv in {p["voltage_mv"] for p in curve}


# --- adjust_baseline_to_measured_clock ------------------------------------


def test_adjust_baseline_returns_candidate_when_clock_unchanged() -> None:
    curve = base_curve(900, 1025, 25, 2000, 40)
    candidate = VfCurveCandidate(
        "baseline-loaded-flattened-curve", 950, 2120, curve
    )
    # avg_core 2160 -> snaps back to the previous lock 2120 (no change).
    gpu = SimpleNamespace(clock_ceiling=None)

    result = undervolt_main_loop.adjust_baseline_to_measured_clock(
        curve,
        candidate=candidate,
        stable_probe=_summary(950, 2160),
        gpu=gpu,
        tail_rise_bins=2,
    )

    assert result is candidate


def test_adjust_baseline_rebuilds_plan_and_retargets_ceiling() -> None:
    curve = base_curve(900, 1025, 25, 2000, 40)
    candidate = VfCurveCandidate(
        "baseline-loaded-flattened-curve", 950, 2120, curve
    )
    captured: dict[str, object] = {}

    class FakeCeiling:
        def retarget(self, *, lock_clock_mhz, lock_voltage_mv, ceiling_clock_mhz):
            captured["lock_clock_mhz"] = lock_clock_mhz
            captured["lock_voltage_mv"] = lock_voltage_mv
            captured["ceiling_clock_mhz"] = ceiling_clock_mhz

    gpu = SimpleNamespace(clock_ceiling=FakeCeiling())

    # avg_core 2000 differs from previous lock 2120 -> rebuild + retarget.
    result = undervolt_main_loop.adjust_baseline_to_measured_clock(
        curve,
        candidate=candidate,
        stable_probe=_summary(950, 2000),
        gpu=gpu,
        tail_rise_bins=2,
    )

    assert result is not candidate
    assert result.label == "baseline-measured-clock-adjusted"
    assert result.target_mhz == 2000
    assert result.voltage_mv == 950
    assert captured["lock_clock_mhz"] == 2000
    assert captured["lock_voltage_mv"] == 950
    assert captured["ceiling_clock_mhz"] >= 2000


def test_adjust_baseline_rebuilds_plan_without_clock_ceiling() -> None:
    curve = base_curve(900, 1025, 25, 2000, 40)
    candidate = VfCurveCandidate(
        "baseline-loaded-flattened-curve", 950, 2120, curve
    )
    gpu = SimpleNamespace(clock_ceiling=None)

    result = undervolt_main_loop.adjust_baseline_to_measured_clock(
        curve,
        candidate=candidate,
        stable_probe=_summary(950, 2000),
        gpu=gpu,
        tail_rise_bins=0,
    )

    assert result.target_mhz == 2000
    assert result.label == "baseline-measured-clock-adjusted"


# --- select_performance_auto_oc_candidate: performance-sweep log branch ---


def test_select_performance_auto_oc_logs_performance_sweep_profile(
    monkeypatch,
) -> None:
    curve = base_curve(900, 1025, 25, 2000, 40)
    log_messages: list[str] = []

    monkeypatch.setattr(
        performance_uv_loop,
        "run_auto_oc_candidate_search",
        lambda **_kwargs: SimpleNamespace(
            selected_candidate=VfCurveCandidate("oc", 950, 2745, curve),
            attempts=(),
        ),
    )

    swept = VfCurveCandidate(
        "oc performance-sweep",
        950,
        2745,
        curve,
        metadata={
            "profile_curve": "performance-sweep",
            "profile_curve_start_voltage_mv": 900,
            "profile_curve_anchor_count": 3,
        },
    )
    monkeypatch.setattr(
        performance_uv_loop,
        "build_performance_sweep_profile_candidate",
        lambda *_args, **_kwargs: swept,
    )

    plan, voltage_mv, clock_mhz, probe, _oc_metadata = (
        performance_uv_loop.select_performance_auto_oc_candidate(
            curve,
            auto_uv_mode="performance",
            stable_plan=curve,
            stable_voltage_mv=925,
            stable_lock_clock_mhz=2600,
            stable_probe=_summary(925, 2600),
            stable_history=[],
            runner=object(),
            gpu_name="NVIDIA GeForce RTX 4090",
            clock_ceiling=None,
            probe_history=[],
            log=log_messages.append,
        )
    )

    assert plan is curve
    assert (voltage_mv, clock_mhz) == (950, 2745)
    # Selection changed -> the prior probe is dropped.
    assert probe is None
    assert any(
        "profile-curve=performance-sweep" in message for message in log_messages
    )
    assert any("start=900mV" in message for message in log_messages)
    assert any("anchors=3" in message for message in log_messages)


def test_select_performance_auto_oc_keeps_probe_when_selection_unchanged(
    monkeypatch,
) -> None:
    curve = base_curve(900, 1025, 25, 2000, 40)
    start_probe = _summary(925, 2600)

    monkeypatch.setattr(
        performance_uv_loop,
        "run_auto_oc_candidate_search",
        lambda **_kwargs: SimpleNamespace(
            selected_candidate=VfCurveCandidate("oc", 925, 2600, curve),
            attempts=(),
        ),
    )
    # Plain selected candidate (no performance-sweep metadata).
    monkeypatch.setattr(
        performance_uv_loop,
        "build_performance_sweep_profile_candidate",
        lambda *_args, **_kwargs: VfCurveCandidate("oc", 925, 2600, curve),
    )

    _plan, voltage_mv, clock_mhz, probe, _oc_metadata = (
        performance_uv_loop.select_performance_auto_oc_candidate(
            curve,
            auto_uv_mode="performance",
            stable_plan=curve,
            stable_voltage_mv=925,
            stable_lock_clock_mhz=2600,
            stable_probe=start_probe,
            stable_history=None,
            runner=object(),
            gpu_name="NVIDIA GeForce RTX 4090",
            clock_ceiling=None,
            probe_history=[],
            log=lambda _message: None,
        )
    )

    assert (voltage_mv, clock_mhz) == (925, 2600)
    # Unchanged selection keeps the original stable probe.
    assert probe is start_probe


def test_performance_auto_oc_progress_metadata_reports_applied_and_limit() -> None:
    metadata = performance_uv_loop.performance_auto_oc_progress_metadata(
        endpoint=SimpleNamespace(clock_mhz=2980),
        measured_baseline_clock_mhz=2600,
        selected_clock_mhz=2745,
    )

    assert metadata["auto_oc"] is True
    assert metadata["auto_oc_limit_mhz"] == 380
    assert metadata["auto_oc_applied_mhz"] == 145
    assert metadata["auto_oc_baseline_clock_mhz"] == 2600
    assert metadata["auto_oc_target_clock_mhz"] == 2980


def test_performance_auto_oc_progress_metadata_keeps_limit_when_no_gain() -> None:
    # Auto-OC reports target minus measured baseline, including a zero delta.
    metadata = performance_uv_loop.performance_auto_oc_progress_metadata(
        endpoint=SimpleNamespace(clock_mhz=2980),
        measured_baseline_clock_mhz=2600,
        selected_clock_mhz=2600,
    )

    assert metadata["auto_oc_applied_mhz"] == 0
    assert metadata["auto_oc_limit_mhz"] == 380


def test_performance_auto_oc_progress_metadata_keeps_signed_delta_above_limit() -> None:
    metadata = performance_uv_loop.performance_auto_oc_progress_metadata(
        endpoint=SimpleNamespace(clock_mhz=2980),
        measured_baseline_clock_mhz=2600,
        selected_clock_mhz=3100,
    )

    assert metadata["auto_oc_applied_mhz"] == 500
    assert metadata["auto_oc_limit_mhz"] == 380


def test_performance_auto_oc_progress_metadata_keeps_negative_delta() -> None:
    metadata = performance_uv_loop.performance_auto_oc_progress_metadata(
        endpoint=SimpleNamespace(clock_mhz=2980),
        measured_baseline_clock_mhz=2730,
        selected_clock_mhz=2600,
    )

    assert metadata["auto_oc_applied_mhz"] == -130
    assert metadata["auto_oc_limit_mhz"] == 250


def test_performance_auto_oc_progress_metadata_empty_without_target() -> None:
    assert (
        performance_uv_loop.performance_auto_oc_progress_metadata(
            endpoint=None,
            measured_baseline_clock_mhz=2600,
            selected_clock_mhz=2700,
        )
        == {}
    )


def test_select_performance_auto_oc_candidate_returns_oc_metadata(monkeypatch) -> None:
    curve = base_curve(900, 1025, 25, 2000, 40)

    monkeypatch.setattr(
        performance_uv_loop,
        "run_auto_oc_candidate_search",
        lambda **_kwargs: SimpleNamespace(
            selected_candidate=VfCurveCandidate("oc", 950, 2745, curve),
            endpoint=SimpleNamespace(clock_mhz=2980),
            attempts=(),
        ),
    )
    monkeypatch.setattr(
        performance_uv_loop,
        "build_performance_sweep_profile_candidate",
        lambda *_args, **_kwargs: VfCurveCandidate("oc", 950, 2745, curve),
    )

    *_unused, oc_metadata = performance_uv_loop.select_performance_auto_oc_candidate(
        curve,
        auto_uv_mode="performance",
        stable_plan=curve,
        stable_voltage_mv=925,
        stable_lock_clock_mhz=2600,
        stable_probe=_summary(925, 2600),
        stable_history=[],
        runner=object(),
        gpu_name="NVIDIA GeForce RTX 4090",
        clock_ceiling=None,
        probe_history=[],
        log=lambda _message: None,
        target_voltage_mv=940,
        target_clock_mhz=2980,
        measured_baseline_clock_mhz=2600,
    )

    assert oc_metadata["auto_oc_applied_mhz"] == 145
    assert oc_metadata["auto_oc_limit_mhz"] == 380


# --- orchestration: discovery / baseline failure guards -------------------


def _orchestration_settings(q2rtx_config, *, mode="efficiency"):
    return SimpleNamespace(
        q2rtx_config=q2rtx_config,
        auto_uv_mode=mode,
        short_probe_base_duration_s=10,
        configured_min_voltage_mv=None,
        configured_max_drop_pct=15.0,
        min_performance_core_clock_pct=90.0,
        final_verification_duration_s=600,
        final_clock_drop_margin_pct=10.0,
        tail_rise_bins=0,
    )


def _orchestration_fake_gpu(curve):
    class FakeGpu:
        reader = object()
        live_voltage_reader = object()
        runtime_default_plan = curve
        power_limit_w = 320
        clock_ceiling = None
        translated_gpu_policy = {}

        def start_clock_ceiling(self, _target) -> None:
            return None

        def close(self) -> None:
            return None

    return FakeGpu()


def test_orchestration_raises_when_discovery_probe_fails(monkeypatch) -> None:
    curve = base_curve(900, 1025, 25, 2000, 40)
    closed = {"closed": False}

    gpu = _orchestration_fake_gpu(curve)
    monkeypatch.setattr(
        type(gpu), "close", lambda self: closed.__setitem__("closed", True)
    )

    monkeypatch.setattr(
        undervolt_main_loop,
        "read_scan_runtime_settings",
        lambda runtime_options, q2rtx_config, gpu_name=None: _orchestration_settings(
            q2rtx_config
        ),
    )
    monkeypatch.setattr(undervolt_main_loop, "consume_crash_cache", lambda **_kwargs: [])
    monkeypatch.setattr(
        undervolt_main_loop, "cleanup_managed_q2rtx_processes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        undervolt_main_loop, "open_live_gpu_vf_curve_applier", lambda **_kwargs: gpu
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "run_discovery_probe",
        lambda *_a, **_k: (
            _summary(1000, 2200),
            SimpleNamespace(success=False, reason="probe crashed"),
        ),
    )

    with pytest.raises(AutoUvError, match="base Defaults baseline failed"):
        undervolt_main_loop.run_voltage_frequency_undervolt_main_loop(
            gpu_index=0,
            runtime_options={},
            q2rtx_config=object(),
            log=lambda _message: None,
        )
    # The finally block still releases the GPU handle.
    assert closed["closed"] is True


def test_orchestration_raises_when_baseline_probe_fails(monkeypatch) -> None:
    curve = base_curve(900, 1025, 25, 2000, 40)
    gpu = _orchestration_fake_gpu(curve)

    class FakeRunner:
        def __init__(self, **_kwargs):
            return None

        def probe_baseline_candidate(self, candidate):
            return VoltageProbeOutcome(
                decision=StableRunDecision(
                    False,
                    FailureKind.CUDA_FAILED,
                    FailureSeverity.UNSAFE,
                    "baseline unstable",
                ),
                measured_core_clock_mhz=float(candidate.target_mhz),
                measured_voltage_mv=float(candidate.voltage_mv),
                raw_probe=_summary(candidate.voltage_mv, candidate.target_mhz),
            )

    monkeypatch.setattr(
        undervolt_main_loop,
        "read_scan_runtime_settings",
        lambda runtime_options, q2rtx_config, gpu_name=None: _orchestration_settings(
            q2rtx_config
        ),
    )
    monkeypatch.setattr(undervolt_main_loop, "consume_crash_cache", lambda **_kwargs: [])
    monkeypatch.setattr(
        undervolt_main_loop, "cleanup_managed_q2rtx_processes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        undervolt_main_loop, "open_live_gpu_vf_curve_applier", lambda **_kwargs: gpu
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "run_discovery_probe",
        lambda *_a, **_k: (_summary(1000, 2200), SimpleNamespace(success=True)),
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "build_loaded_baseline_candidate",
        lambda *_a, **_k: (
            VfCurveCandidate("baseline", 1000, 2200, curve),
            SimpleNamespace(measured_clock_mhz=2200.0),
        ),
    )
    monkeypatch.setattr(undervolt_main_loop, "AutoUvProbeRunner", FakeRunner)
    monkeypatch.setattr(
        undervolt_main_loop,
        "build_flatten_target_for_plan",
        lambda *_a, **_k: {"lock_clock_mhz": 2200, "ceiling_clock_mhz": 2230},
    )

    with pytest.raises(AutoUvError, match="baseline flattened curve failed"):
        undervolt_main_loop.run_voltage_frequency_undervolt_main_loop(
            gpu_index=0,
            runtime_options={},
            q2rtx_config=object(),
            log=lambda _message: None,
        )


def test_orchestration_keyboard_interrupt_reraises_without_final_choice(
    monkeypatch,
) -> None:
    curve = base_curve(900, 1025, 25, 2000, 40)
    gpu = _orchestration_fake_gpu(curve)

    class FakeRunner:
        def __init__(self, **_kwargs):
            return None

        def probe_baseline_candidate(self, candidate):
            return VoltageProbeOutcome(
                decision=StableRunDecision(
                    True, FailureKind.NONE, FailureSeverity.PASS, "stable run"
                ),
                measured_core_clock_mhz=float(candidate.target_mhz),
                measured_voltage_mv=float(candidate.voltage_mv),
                raw_probe=_summary(candidate.voltage_mv, candidate.target_mhz),
            )

    monkeypatch.setattr(
        undervolt_main_loop,
        "read_scan_runtime_settings",
        lambda runtime_options, q2rtx_config, gpu_name=None: _orchestration_settings(
            q2rtx_config
        ),
    )
    monkeypatch.setattr(undervolt_main_loop, "consume_crash_cache", lambda **_kwargs: [])
    monkeypatch.setattr(
        undervolt_main_loop, "cleanup_managed_q2rtx_processes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        undervolt_main_loop, "open_live_gpu_vf_curve_applier", lambda **_kwargs: gpu
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "run_discovery_probe",
        lambda *_a, **_k: (_summary(1000, 2200), SimpleNamespace(success=True)),
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "build_loaded_baseline_candidate",
        lambda *_a, **_k: (
            VfCurveCandidate("baseline", 1000, 2200, curve),
            SimpleNamespace(measured_clock_mhz=2200.0),
        ),
    )
    monkeypatch.setattr(undervolt_main_loop, "AutoUvProbeRunner", FakeRunner)
    monkeypatch.setattr(
        undervolt_main_loop,
        "adjust_baseline_to_measured_clock",
        lambda _base_curve, *, candidate, **_kwargs: candidate,
    )
    monkeypatch.setattr(
        undervolt_main_loop, "write_verified_candidate", lambda *_a, **_k: None
    )

    def fake_sweep_loop(*_args, **_kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(
        undervolt_main_loop, "run_preset_uv_loop", fake_sweep_loop
    )

    with pytest.raises(KeyboardInterrupt):
        undervolt_main_loop.run_voltage_frequency_undervolt_main_loop(
            gpu_index=0,
            runtime_options={},  # no auto_uv_require_final_choice -> re-raise
            q2rtx_config=object(),
            log=lambda _message: None,
        )


def test_orchestration_sweep_hooks_probe_and_record_candidates(monkeypatch) -> None:
    # Exercises the probe_candidate / record_passed_candidate closures, the
    # clock-ceiling retarget for a sweep candidate, and the
    # enforce_target_core_clock_floor=False path triggered by explicit flags.
    curve = base_curve(900, 1025, 25, 2000, 40)
    captured: dict[str, object] = {"sweep_kwargs": [], "probe_history_lens": []}

    class FakeCeiling:
        def retarget(self, **kwargs):
            captured.setdefault("retargets", []).append(kwargs)

    gpu = _orchestration_fake_gpu(curve)
    monkeypatch.setattr(type(gpu), "clock_ceiling", FakeCeiling())

    class FakeRunner:
        def __init__(self, **_kwargs):
            return None

        def probe_baseline_candidate(self, candidate):
            return VoltageProbeOutcome(
                decision=StableRunDecision(
                    True, FailureKind.NONE, FailureSeverity.PASS, "stable run"
                ),
                measured_core_clock_mhz=float(candidate.target_mhz),
                measured_voltage_mv=float(candidate.voltage_mv),
                raw_probe=_summary(candidate.voltage_mv, candidate.target_mhz),
            )

        def probe_sweep_candidate(self, candidate, **kwargs):
            captured["sweep_kwargs"].append(dict(kwargs))
            return VoltageProbeOutcome(
                decision=StableRunDecision(
                    True, FailureKind.NONE, FailureSeverity.PASS, "stable run"
                ),
                measured_core_clock_mhz=float(candidate.target_mhz),
                measured_voltage_mv=float(candidate.voltage_mv),
                raw_probe=_summary(candidate.voltage_mv, candidate.target_mhz),
            )

    def fake_sweep_loop(
        _base_curve,
        *,
        settings,
        initial_stable_candidate,
        io,
        unsafe_entries,
        initial_stable_outcome=None,
        **_kwargs,
    ):
        _ = settings, unsafe_entries, initial_stable_outcome
        sweep_candidate = VfCurveCandidate("sweep", 925, 2080, curve)
        # Drive the probe_candidate closure (lines 246-256).
        outcome = io.probe_candidate(sweep_candidate)
        assert outcome.decision.passed is True
        # Drive the record_passed_candidate closure (lines 278-279).
        io.record_passed_candidate(sweep_candidate, outcome)
        # Drive the accept path so stable history advances.
        io.write_verified_candidate(sweep_candidate, outcome)
        return LowerVoltageSweepResult(
            stable_candidate=sweep_candidate,
            stable_outcome=outcome,
            state=VoltageSweepState(
                stable_voltage_mv=925,
                stable_target_mhz=2080,
                next_voltage_mv=None,
            ),
        )

    monkeypatch.setattr(
        undervolt_main_loop,
        "read_scan_runtime_settings",
        lambda runtime_options, q2rtx_config, gpu_name=None: _orchestration_settings(
            q2rtx_config
        ),
    )
    monkeypatch.setattr(undervolt_main_loop, "consume_crash_cache", lambda **_kwargs: [])
    monkeypatch.setattr(
        undervolt_main_loop, "cleanup_managed_q2rtx_processes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        undervolt_main_loop, "open_live_gpu_vf_curve_applier", lambda **_kwargs: gpu
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "run_discovery_probe",
        lambda *_a, **_k: (_summary(1000, 2200), SimpleNamespace(success=True)),
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "build_loaded_baseline_candidate",
        lambda *_a, **_k: (
            VfCurveCandidate("baseline", 1000, 2200, curve),
            SimpleNamespace(measured_clock_mhz=2200.0),
        ),
    )
    monkeypatch.setattr(undervolt_main_loop, "AutoUvProbeRunner", FakeRunner)
    monkeypatch.setattr(
        undervolt_main_loop,
        "adjust_baseline_to_measured_clock",
        lambda _base_curve, *, candidate, **_kwargs: candidate,
    )
    monkeypatch.setattr(
        undervolt_main_loop, "write_verified_candidate", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        undervolt_main_loop, "run_preset_uv_loop", fake_sweep_loop
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "build_flatten_target_for_plan",
        lambda *_a, **_k: {"lock_clock_mhz": 2200, "ceiling_clock_mhz": 2230},
    )
    monkeypatch.setattr(
        undervolt_main_loop, "run_final_verification_and_save", lambda **_k: "done"
    )

    result = undervolt_main_loop.run_voltage_frequency_undervolt_main_loop(
        gpu_index=0,
        runtime_options={},
        q2rtx_config=object(),
        log=lambda _message: None,
    )

    assert result == "done"
    # The sweep candidate probe keeps the clock floor enforced (runner default):
    # pass 1 must stop at the floor so the tail-tune pass owns the deep region.
    assert captured["sweep_kwargs"]
    assert (
        "enforce_target_core_clock_floor" not in captured["sweep_kwargs"][0]
    )
    # The candidate's clock ceiling was retargeted for the sweep candidate.
    assert captured.get("retargets")


def test_adaptive_tier_power_limit_scales_from_the_balanced_anchor() -> None:
    from auto_uv.main_loop import adaptive_tier_power_limit_w

    # No table entry for this GPU: the scan-wide request passes through.
    assert (
        adaptive_tier_power_limit_w(
            power_limit_pct=None,
            baseline_power_limit_w=360,
            scan_request_w=320,
            balanced_pct=None,
        )
        == 320
    )
    # Untouched slider: each tier gets its uv_limits share of the stock
    # budget (5080: efficiency 77.44% of 360W).
    assert (
        adaptive_tier_power_limit_w(
            power_limit_pct=77.44,
            baseline_power_limit_w=360,
            scan_request_w=None,
            balanced_pct=88.72,
        )
        == 279
    )
    # A manual slider value is the balanced anchor: every tier scales
    # proportionally around it.
    assert (
        adaptive_tier_power_limit_w(
            power_limit_pct=77.44,
            baseline_power_limit_w=360,
            scan_request_w=300,
            balanced_pct=88.72,
        )
        == 262
    )
    # Never above the stock board budget.
    assert (
        adaptive_tier_power_limit_w(
            power_limit_pct=100.0,
            baseline_power_limit_w=360,
            scan_request_w=400,
            balanced_pct=88.72,
        )
        == 360
    )


def test_adaptive_tier_order_and_descent_tails() -> None:
    from auto_uv.main_loop import (
        ADAPTIVE_TIER_ORDER,
        adaptive_tier_descent_tail_rise_bins,
    )
    from auto_uv.domain.user_options import AUTO_UV_DEFAULTS

    # Efficiency first: it descends deepest (most fragile), so it soaks the
    # full final duration while shallower tiers get the graduated confirm.
    assert ADAPTIVE_TIER_ORDER == ("efficiency", "balanced", "performance")
    # Each tier descends WITH its own tail — the tail compounds through the
    # measured-clock ratchet, so it cannot be decorated on after a tail-less
    # descent. Efficiency descends tail-less in pass 1 (its loop raises +2 in
    # the tail-tune pass); balanced/performance carry their full tail.
    assert adaptive_tier_descent_tail_rise_bins("efficiency") == int(
        AUTO_UV_DEFAULTS.tail_rise_bins
    )
    assert adaptive_tier_descent_tail_rise_bins("balanced") == int(
        AUTO_UV_DEFAULTS.balanced_tail_rise_bins
    )
    assert adaptive_tier_descent_tail_rise_bins("performance") == int(
        AUTO_UV_DEFAULTS.performance_tail_rise_bins
    )


def test_adaptive_tier_progress_events_are_chronological(monkeypatch) -> None:
    import auto_uv.main_loop as main_loop

    events = []
    tier_index = {
        "efficiency": 0,
        "balanced": 1,
        "performance": 2,
    }

    def fake_descent(_base_curve, *, tier_mode, **_kwargs):
        index = tier_index[tier_mode]
        candidate = VfCurveCandidate(
            tier_mode,
            900 + index * 10,
            2500 + index * 20,
            [],
        )
        return candidate, index * 2, object(), []

    def fake_selection(**kwargs):
        return SimpleNamespace(
            plan=list(kwargs["stable_plan"]),
            voltage_mv=int(kwargs["stable_voltage_mv"]),
            lock_clock_mhz=int(kwargs["stable_lock_clock_mhz"]),
            probe=kwargs["stable_probe"],
            verification_duration_s=int(kwargs["final_verification_duration_s"]),
            tail_rise_bins=int(kwargs["tail_rise_bins"]),
            auto_oc_metadata={},
        )

    def fake_finish(**kwargs):
        return SimpleNamespace(
            final_voltage_mv=int(kwargs["final_stable_voltage_mv"]),
            lock_clock_mhz=int(kwargs["final_stable_lock_clock_mhz"]),
        )

    monkeypatch.setattr(main_loop, "run_adaptive_tier_descent", fake_descent)
    monkeypatch.setattr(main_loop, "select_final_scan_candidate", fake_selection)
    monkeypatch.setattr(
        main_loop, "apply_adaptive_tier_power_limit", lambda *_args, **_kwargs: None
    )

    result = main_loop.run_adaptive_tier_scans(
        base_curve=[],
        gpu=SimpleNamespace(
            power_limit_w=360,
            requested_power_limit_w=None,
            translated_gpu_policy={"gpu_name": "Test GPU"},
            policy_controller=SimpleNamespace(
                get_memory_clock_offset_range_mhz=lambda: (0, 4000)
            ),
        ),
        configure_tier_probe_runner=lambda _min_core_clock_pct: object(),
        settings=SimpleNamespace(),
        runtime_options={},
        base_loop_settings=SimpleNamespace(),
        baseline_candidate=VfCurveCandidate("baseline", 1000, 2400, []),
        initial_stable_outcome=None,
        stable_probe=None,
        discovery_summary=object(),
        probe_history=[],
        baseline_target=SimpleNamespace(measured_clock_mhz=2400),
        effective_min_search_voltage_mv=800,
        final_verification_duration_s=300,
        unsafe_entries=[],
        probe_candidate=lambda _candidate: None,
        finish_with_final_verification=fake_finish,
        event_callback=lambda event, payload: events.append((event, payload)),
        log=lambda _message: None,
    )

    assert result.final_voltage_mv == 900
    # Each tier also announces its active memory offset; only the tier
    # progress events matter for the chronology here.
    assert [
        event for event, _payload in events if event.startswith("tier_")
    ] == [
        "tier_started",
        "tier_completed",
        "tier_started",
        "tier_completed",
        "tier_started",
        "tier_completed",
    ]
    started = [payload for event, payload in events if event == "tier_started"]
    assert [payload["tier"] for payload in started] == [
        "efficiency",
        "balanced",
        "performance",
    ]
    assert [payload["position"] for payload in started] == [1, 2, 3]
    assert [payload.get("next_tier", "") for payload in started] == [
        "balanced",
        "performance",
        "",
    ]


def test_clamp_power_limit_keeps_request_inside_card_range() -> None:
    from auto_uv.gpu.gpu_vf_curve_applier import LiveGpuVfCurveApplier

    applier = LiveGpuVfCurveApplier(
        gpu_index=0,
        gpu=SimpleNamespace(),
        runtime_default_plan=[],
        translated_gpu_policy={"power_limit_w": 360},
        min_power_limit_w=300,
        max_power_limit_w=390,
    )
    # An efficiency tier's 279W computed cap clamps UP to the 300W hardware
    # floor instead of being rejected by NVML.
    assert applier.clamp_power_limit_w(279) == 300
    assert applier.clamp_power_limit_w(420) == 390
    assert applier.clamp_power_limit_w(330) == 330


def test_graduated_final_verification_durations() -> None:
    from auto_uv.main_loop import (
        final_verification_duration_for_tier,
        SECONDARY_FINAL_VERIFICATION_S,
    )

    # The deepest (index 0) profile soaks the full configured duration; the
    # shallower tiers get the shorter confirm, never longer than configured.
    assert final_verification_duration_for_tier(0, configured_final_duration_s=300) == 300
    assert (
        final_verification_duration_for_tier(1, configured_final_duration_s=300)
        == SECONDARY_FINAL_VERIFICATION_S
    )
    assert final_verification_duration_for_tier(2, configured_final_duration_s=60) == 60
    # The Auto-OC tier's climbed point is new territory, never a re-proven
    # trunk edge, so it always soaks the full configured duration.
    assert (
        final_verification_duration_for_tier(
            2, configured_final_duration_s=300, runs_auto_oc=True
        )
        == 300
    )


def test_adaptive_tier_clock_drop_margin_resolution_order() -> None:
    from auto_uv.run.scan_runtime_settings import (
        adaptive_tier_clock_drop_margin_pct,
    )
    from auto_uv.domain.user_options import AUTO_UV_DEFAULTS

    # The tier's own option wins over the scan-wide key.
    assert (
        adaptive_tier_clock_drop_margin_pct(
            {
                "auto_uv_balanced_max_clock_drop_pct": 6.0,
                "auto_uv_max_clock_drop_pct": 15.0,
            },
            tier_mode="balanced",
            gpu_name="Unknown GPU",
        )
        == 6.0
    )
    # Without a per-tier key the scan-wide key still applies to every tier.
    assert (
        adaptive_tier_clock_drop_margin_pct(
            {"auto_uv_max_clock_drop_pct": 12.0},
            tier_mode="performance",
            gpu_name="Unknown GPU",
        )
        == 12.0
    )
    # No options at all: the GPU table per-tier limit, then the generic
    # fallback for unknown GPUs.
    assert adaptive_tier_clock_drop_margin_pct(
        {},
        tier_mode="efficiency",
        gpu_name="Unknown GPU",
    ) == float(AUTO_UV_DEFAULTS.max_core_clock_drop_pct)
    # Known GPU: each tier resolves ITS table row, so balanced no longer
    # inherits the efficiency (loosest) allowance.
    from auto_uv.scan_mode.uv_limits import uv_limit_clock_drop_pct_for_gpu

    gpu_name = "NVIDIA GeForce RTX 5080"
    for tier in ("efficiency", "balanced", "performance"):
        expected = uv_limit_clock_drop_pct_for_gpu(gpu_name, profile_id=tier)
        if expected is None:
            continue
        assert adaptive_tier_clock_drop_margin_pct(
            {},
            tier_mode=tier,
            gpu_name=gpu_name,
        ) == float(expected)


def test_apply_adaptive_tier_power_limit_explicit_watts_wins() -> None:
    from auto_uv.main_loop import apply_adaptive_tier_power_limit

    gpu = SimpleNamespace(
        requested_power_limit_w=None,
        clamp_power_limit_w=lambda watts: max(300, min(400, int(watts))),
    )
    # An explicit per-tier request bypasses the balanced-anchor scaling
    # (clamped to the card's NVML range).
    apply_adaptive_tier_power_limit(
        gpu,
        tier_mode="performance",
        stock_power_limit_w=360,
        scan_request_w=None,
        balanced_pct=85.0,
        explicit_watts=390,
    )
    assert gpu.requested_power_limit_w == 390

    apply_adaptive_tier_power_limit(
        gpu,
        tier_mode="efficiency",
        stock_power_limit_w=360,
        scan_request_w=None,
        balanced_pct=85.0,
        explicit_watts=250,
    )
    assert gpu.requested_power_limit_w == 300  # NVML floor clamp

    # A manual scan-wide request stays a hard ceiling even over an explicit
    # per-tier flag: mixed CLI invocations may not exceed it.
    apply_adaptive_tier_power_limit(
        gpu,
        tier_mode="performance",
        stock_power_limit_w=360,
        scan_request_w=320,
        balanced_pct=85.0,
        explicit_watts=390,
    )
    assert gpu.requested_power_limit_w == 320


def test_full_scan_open_paths_fall_back_to_per_tier_options() -> None:
    from auto_uv.gpu.gpu_vf_curve_applier import _auto_uv_power_limit_w
    from auto_uv.gpu.memory_clock_offset_user_option import (
        auto_uv_memory_offset_mhz,
    )

    # Scan open uses the HIGHEST per-tier power request when the scan-wide
    # key is absent, so a raised cap holds during discovery and descents.
    assert (
        _auto_uv_power_limit_w(
            {
                "auto_uv_efficiency_power_limit_w": 250,
                "auto_uv_balanced_power_limit_w": 300,
                "auto_uv_performance_power_limit_w": 390,
            }
        )
        == 390
    )
    assert _auto_uv_power_limit_w({"auto_uv_power_limit_w": 320}) == 320
    assert _auto_uv_power_limit_w({}) is None

    # Scan open applies the FIRST (efficiency) tier's memory offset when the
    # scan-wide key is absent, so the shared baseline runs under tier one's
    # memory clock.
    offset, _limit = auto_uv_memory_offset_mhz(
        {"auto_uv_efficiency_memory_offset_mhz": 500},
        policy_controller=SimpleNamespace(
            get_memory_clock_offset_range_mhz=lambda: (0, 4000)
        ),
    )
    assert offset == 500
    offset, _limit = auto_uv_memory_offset_mhz(
        {},
        policy_controller=SimpleNamespace(
            get_memory_clock_offset_range_mhz=lambda: (0, 4000)
        ),
    )
    assert offset is None


def test_apply_adaptive_tier_memory_offset_applies_per_tier_value() -> None:
    from auto_uv.main_loop import apply_adaptive_tier_memory_offset

    applied = []
    events = []
    gpu = SimpleNamespace(
        gpu=SimpleNamespace(
            apply_clock_offsets=lambda mem_clk_vf_offset_mhz: applied.append(
                mem_clk_vf_offset_mhz
            )
            or {"mem_clk_vf_offset_readback_mhz": mem_clk_vf_offset_mhz},
        ),
        translated_gpu_policy={"gpu_name": "Test GPU"},
    )

    def tier_offset(tier_mode, runtime_options, fallback_offset_mhz=0):
        apply_adaptive_tier_memory_offset(
            gpu,
            tier_mode=tier_mode,
            runtime_options=runtime_options,
            fallback_offset_mhz=fallback_offset_mhz,
            limit_mhz=4000,
            event_callback=lambda event, payload: events.append((event, payload)),
            log=lambda _message: None,
        )

    # Absent per-tier option with a matching zero fallback: no NVML write.
    tier_offset("efficiency", {})
    assert applied == []
    assert gpu.translated_gpu_policy["mem_clk_vf_offset_mhz"] == 0

    # Per-tier value: applied, recorded in the policy (so the tier's profile
    # saves it), and clamped to the driver range.
    tier_offset("balanced", {"auto_uv_balanced_memory_offset_mhz": 9999})
    assert applied == [4000]
    assert gpu.translated_gpu_policy["mem_clk_vf_offset_mhz"] == 4000
    assert events[-1][0] == "memory_offset_applied"
    assert events[-1][1]["offset_mt_s"] == 4000
    assert events[-1][1]["tier"] == "balanced"

    # A tier WITHOUT its own option restores the scan-open fallback instead of
    # inheriting the previous tier's offset.
    tier_offset("performance", {}, fallback_offset_mhz=500)
    assert applied == [4000, 500]
    assert gpu.translated_gpu_policy["mem_clk_vf_offset_mhz"] == 500
    assert events[-1][1]["tier"] == "performance"

    # Same value again: no redundant NVML write.
    tier_offset("performance", {"auto_uv_performance_memory_offset_mhz": 500})
    assert applied == [4000, 500]

    # A zero offset after a nonzero one clears the applied offset.
    tier_offset("performance", {"auto_uv_performance_memory_offset_mhz": 0})
    assert applied == [4000, 500, 0]
    assert gpu.translated_gpu_policy["mem_clk_vf_offset_mhz"] == 0


def test_apply_adaptive_tier_memory_offset_records_readback_and_survives_errors() -> (
    None
):
    from auto_uv.main_loop import apply_adaptive_tier_memory_offset

    events = []
    # Driver clamps the request: the policy and event must record the NVML
    # read-back (what is actually live), not the requested value.
    gpu = SimpleNamespace(
        gpu=SimpleNamespace(
            apply_clock_offsets=lambda mem_clk_vf_offset_mhz: {
                "mem_clk_vf_offset_readback_mhz": 3000
            },
        ),
        translated_gpu_policy={},
    )
    apply_adaptive_tier_memory_offset(
        gpu,
        tier_mode="balanced",
        runtime_options={"auto_uv_balanced_memory_offset_mhz": 4000},
        fallback_offset_mhz=0,
        limit_mhz=6000,
        event_callback=lambda event, payload: events.append((event, payload)),
        log=lambda _message: None,
    )
    assert gpu.translated_gpu_policy["mem_clk_vf_offset_mhz"] == 3000
    assert events[-1][1]["offset_mt_s"] == 3000

    # An apply failure keeps the previous offset and does NOT raise: one
    # tier's NVML hiccup must not abort the whole adaptive scan.
    def raise_apply(**_kwargs):
        raise RuntimeError("transient NVML error")

    gpu = SimpleNamespace(
        gpu=SimpleNamespace(apply_clock_offsets=raise_apply),
        translated_gpu_policy={"mem_clk_vf_offset_mhz": 500},
    )
    apply_adaptive_tier_memory_offset(
        gpu,
        tier_mode="performance",
        runtime_options={"auto_uv_performance_memory_offset_mhz": 1000},
        fallback_offset_mhz=0,
        limit_mhz=6000,
        event_callback=lambda event, payload: events.append((event, payload)),
        log=lambda _message: None,
    )
    assert gpu.translated_gpu_policy["mem_clk_vf_offset_mhz"] == 500


def test_adaptive_tiers_flow_per_tier_limits_into_probe_and_selection(
    monkeypatch,
) -> None:
    import auto_uv.main_loop as main_loop

    configured_floors = []
    selection_floors = []
    finish_margins = []
    tier_power_requests = []
    events = []

    def fake_descent(_base_curve, *, tier_mode, min_core_clock_pct, **_kwargs):
        configured_floors.append((tier_mode, min_core_clock_pct))
        return VfCurveCandidate(tier_mode, 900, 2500, []), 2, object(), []

    def fake_selection(**kwargs):
        selection_floors.append(kwargs["min_core_clock_pct_override"])
        return SimpleNamespace(
            plan=list(kwargs["stable_plan"]),
            voltage_mv=int(kwargs["stable_voltage_mv"]),
            lock_clock_mhz=int(kwargs["stable_lock_clock_mhz"]),
            probe=kwargs["stable_probe"],
            verification_duration_s=int(kwargs["final_verification_duration_s"]),
            tail_rise_bins=int(kwargs["tail_rise_bins"]),
            auto_oc_metadata={},
        )

    def fake_finish(**kwargs):
        finish_margins.append(
            (
                kwargs["final_min_core_clock_pct"],
                kwargs["final_clock_drop_margin_pct"],
            )
        )
        tier_power_requests.append(gpu.requested_power_limit_w)
        return SimpleNamespace(
            final_voltage_mv=int(kwargs["final_stable_voltage_mv"]),
            lock_clock_mhz=int(kwargs["final_stable_lock_clock_mhz"]),
        )

    monkeypatch.setattr(main_loop, "run_adaptive_tier_descent", fake_descent)
    monkeypatch.setattr(main_loop, "select_final_scan_candidate", fake_selection)

    gpu = SimpleNamespace(
        power_limit_w=360,
        requested_power_limit_w=None,
        translated_gpu_policy={"gpu_name": "Unknown GPU"},
        clamp_power_limit_w=lambda watts: int(watts),
        policy_controller=SimpleNamespace(
            get_memory_clock_offset_range_mhz=lambda: (0, 4000)
        ),
    )

    main_loop.run_adaptive_tier_scans(
        base_curve=[],
        gpu=gpu,
        configure_tier_probe_runner=lambda pct: SimpleNamespace(
            min_performance_core_clock_pct=pct
        ),
        settings=SimpleNamespace(),
        runtime_options={
            "auto_uv_efficiency_max_clock_drop_pct": 15.0,
            "auto_uv_balanced_max_clock_drop_pct": 6.0,
            "auto_uv_performance_max_clock_drop_pct": 5.4,
            "auto_uv_efficiency_power_limit_w": 250,
            "auto_uv_balanced_power_limit_w": 300,
            "auto_uv_performance_power_limit_w": 360,
        },
        base_loop_settings=SimpleNamespace(),
        baseline_candidate=VfCurveCandidate("baseline", 1000, 2400, []),
        initial_stable_outcome=None,
        stable_probe=None,
        discovery_summary=object(),
        probe_history=[],
        baseline_target=SimpleNamespace(measured_clock_mhz=2400),
        effective_min_search_voltage_mv=800,
        final_verification_duration_s=300,
        unsafe_entries=[],
        probe_candidate=lambda _candidate: None,
        finish_with_final_verification=fake_finish,
        event_callback=lambda event, payload: events.append((event, payload)),
        log=lambda _message: None,
    )

    # Each tier's descent, probe runner, selection, and final verification all
    # see the tier's OWN clock-drop allowance.
    assert configured_floors == [
        ("efficiency", 85.0),
        ("balanced", 94.0),
        ("performance", 94.6),
    ]
    assert selection_floors == [85.0, 94.0, 94.6]
    assert [margin for _floor, margin in finish_margins] == [15.0, 6.0, 5.4]
    assert [floor for floor, _margin in finish_margins] == [85.0, 94.0, 94.6]
    # Each tier's explicit power request is applied before its verification.
    assert tier_power_requests == [250, 300, 360]
