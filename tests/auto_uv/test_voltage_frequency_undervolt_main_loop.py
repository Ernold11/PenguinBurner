from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

from auto_uv.auto_uv_types import (
    AutoUvProbeSummary,
    FailureKind,
    FailureSeverity,
    StableRunDecision,
    VfCurveCandidate,
)
from auto_uv import voltage_frequency_undervolt_main_loop as undervolt_main_loop
from auto_uv.final_verification.main_loop import (
    run_final_verification_and_save as real_run_final_verification_and_save,
)
from auto_uv.voltage_sweep_state import (
    LowerVoltageSweepResult,
    VoltageProbeOutcome,
    VoltageSweepState,
)
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


def test_efficiency_tail_tune_uses_balanced_bins_unless_tail_bins_were_explicit() -> None:
    assert (
        undervolt_main_loop.efficiency_tail_tune_tail_rise_bins(
            {},
            descent_tail_rise_bins=0,
        )
        == 4
    )
    assert (
        undervolt_main_loop.efficiency_tail_tune_tail_rise_bins(
            {"auto_uv_tail_rise_bins_explicit": True},
            descent_tail_rise_bins=2,
        )
        == 2
    )
    assert (
        undervolt_main_loop.efficiency_tail_tune_tail_rise_bins(
            {"auto_uv_tail_rise_bins_explicit": True},
            descent_tail_rise_bins=0,
        )
        == 0
    )


def test_explicit_zero_tail_descent_does_not_enforce_clock_floor() -> None:
    assert (
        undervolt_main_loop.lower_voltage_descent_enforces_clock_floor(
            {
                "auto_uv_tail_rise_bins_explicit": True,
                "auto_uv_min_voltage_mv_explicit": True,
            },
            tail_rise_bins=0,
        )
        is False
    )
    assert (
        undervolt_main_loop.lower_voltage_descent_enforces_clock_floor(
            {
                "auto_uv_tail_rise_bins_explicit": True,
                "auto_uv_min_voltage_mv_explicit": True,
            },
            tail_rise_bins=2,
        )
        is True
    )
    assert (
        undervolt_main_loop.lower_voltage_descent_enforces_clock_floor(
            {},
            tail_rise_bins=0,
        )
        is True
    )
    assert (
        undervolt_main_loop.lower_voltage_descent_enforces_clock_floor(
            {"auto_uv_tail_rise_bins_explicit": True},
            tail_rise_bins=0,
        )
        is True
    )


def test_discovery_probe_runner_uses_live_voltage_reader_keyword(monkeypatch) -> None:
    captured = {}

    class FakeGpu:
        reader = object()
        live_voltage_reader = object()
        runtime_default_plan = []
        power_limit_w = 360

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

    monkeypatch.setattr(undervolt_main_loop, "Q2RtxCudaProbeRunner", FakeRunner)
    monkeypatch.setattr(
        undervolt_main_loop,
        "emit_ui_json_event",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "probe_summary_ui_payload",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(undervolt_main_loop, "log_benchmark", lambda *args, **kwargs: None)

    undervolt_main_loop.run_discovery_probe(
        base_curve(900, 1025, 25, 2000, 40),
        gpu=FakeGpu(),
        q2rtx_config=object(),
        short_probe_base_duration_s=10,
        timedemo_warmup_runs=0,
        log=lambda _message: None,
        event_callback=None,
    )

    assert captured["live_voltage_reader"] is FakeGpu.live_voltage_reader
    assert captured["label_voltage_mv"] == 1000


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

    monkeypatch.setattr(undervolt_main_loop, "Q2RtxCudaProbeRunner", FakeRunner)
    monkeypatch.setattr(
        undervolt_main_loop,
        "emit_ui_json_event",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        undervolt_main_loop,
        "probe_summary_ui_payload",
        lambda *args, **kwargs: {},
    )

    undervolt_main_loop.run_discovery_probe(
        base_curve(900, 1025, 25, 2000, 40),
        gpu=FakeGpu(),
        q2rtx_config=object(),
        short_probe_base_duration_s=10,
        timedemo_warmup_runs=0,
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
        hooks,
        unsafe_entries,
        initial_stable_outcome=None,
    ):
        _ = unsafe_entries, initial_stable_outcome
        captured["sweep_calls"].append(
            (
                str(settings.auto_uv_mode),
                int(settings.tail_rise_bins),
                int(initial_stable_candidate.voltage_mv),
                int(initial_stable_candidate.target_mhz),
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
        hooks.write_verified_candidate(candidate, outcome)
        return LowerVoltageSweepResult(
            stable_candidate=candidate,
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
            timedemo_warmup_runs=0,
            short_probe_base_duration_s=10,
            configured_min_voltage_mv=None,
            configured_max_drop_pct=15.0,
            preserve_base_below_mv=None,
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
    monkeypatch.setattr(undervolt_main_loop, "run_lower_voltage_sweep_loop", fake_sweep_loop)
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
    assert captured["sweep_calls"] == [
        ("efficiency", 0, 1000, 2200),
        ("efficiency-tail-tune", 4, 950, 2120),
    ]
    assert captured["final_tail_rise_bins"] == 4


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
        hooks,
        unsafe_entries,
        initial_stable_outcome=None,
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
        hooks.write_verified_candidate(candidate, outcome)
        return LowerVoltageSweepResult(
            stable_candidate=candidate,
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
            timedemo_warmup_runs=0,
            short_probe_base_duration_s=10,
            configured_min_voltage_mv=None,
            configured_max_drop_pct=15.0,
            preserve_base_below_mv=None,
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
    monkeypatch.setattr(undervolt_main_loop, "run_lower_voltage_sweep_loop", fake_sweep_loop)
    monkeypatch.setattr(
        undervolt_main_loop,
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
    assert captured["final"] == (910, 2890)


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
            timedemo_warmup_runs=0,
            short_probe_base_duration_s=10,
            configured_min_voltage_mv=None,
            configured_max_drop_pct=15.0,
            preserve_base_below_mv=None,
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
    monkeypatch.setattr(undervolt_main_loop, "run_lower_voltage_sweep_loop", fake_sweep_loop)
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


def test_performance_auto_oc_selection_runs_before_final_verification(monkeypatch) -> None:
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
        undervolt_main_loop,
        "run_auto_oc_candidate_search",
        fake_auto_oc_search,
    )

    plan, voltage_mv, clock_mhz, probe = (
        undervolt_main_loop.select_performance_auto_oc_candidate(
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
        undervolt_main_loop,
        "run_auto_oc_candidate_search",
        fail_auto_oc_search,
    )

    plan, voltage_mv, clock_mhz, probe = (
        undervolt_main_loop.select_performance_auto_oc_candidate(
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
