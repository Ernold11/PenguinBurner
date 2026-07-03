from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace

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


def test_zero_tail_descent_does_not_enforce_clock_floor() -> None:
    assert (
        undervolt_main_loop.lower_voltage_descent_enforces_clock_floor(
            tail_rise_bins=0,
        )
        is False
    )
    assert (
        undervolt_main_loop.lower_voltage_descent_enforces_clock_floor(
            tail_rise_bins=2,
        )
        is True
    )


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

    monkeypatch.setattr(baseline_probe, "Q2RtxCudaProbeRunner", FakeRunner)
    monkeypatch.setattr(
        baseline_probe,
        "emit_ui_json_event",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        baseline_probe,
        "probe_summary_ui_payload",
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

    monkeypatch.setattr(baseline_probe, "Q2RtxCudaProbeRunner", FakeRunner)
    monkeypatch.setattr(
        baseline_probe,
        "emit_ui_json_event",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        baseline_probe,
        "probe_summary_ui_payload",
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
    monkeypatch.setattr(undervolt_main_loop, "Q2RtxCudaProbeRunner", FakeRunner)
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
    monkeypatch.setattr(undervolt_main_loop, "Q2RtxCudaProbeRunner", FakeRunner)
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
    captured: dict[str, object] = {"order": []}

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
    monkeypatch.setattr(undervolt_main_loop, "Q2RtxCudaProbeRunner", FakeRunner)
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
    assert captured["order"] == ["auto-oc", "choice", "final"]
    assert captured["auto_oc_start"] == (870, 2741)
    assert captured["choice_stable"] == (910, 2890)
    assert (910, 2890) in captured["choice_history"]
    assert captured["final"] == (870, 2595)


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
    monkeypatch.setattr(undervolt_main_loop, "Q2RtxCudaProbeRunner", FakeRunner)
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
    monkeypatch.setattr(undervolt_main_loop, "Q2RtxCudaProbeRunner", FakeRunner)
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
    monkeypatch.setattr(undervolt_main_loop, "Q2RtxCudaProbeRunner", FakeRunner)
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
    monkeypatch.setattr(undervolt_main_loop, "Q2RtxCudaProbeRunner", FakeRunner)
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
    monkeypatch.setattr(undervolt_main_loop, "Q2RtxCudaProbeRunner", FakeRunner)
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
    # The sweep candidate probe disabled the clock floor.
    assert captured["sweep_kwargs"]
    assert (
        captured["sweep_kwargs"][0].get("enforce_target_core_clock_floor") is False
    )
    # The candidate's clock ceiling was retargeted for the sweep candidate.
    assert captured.get("retargets")
