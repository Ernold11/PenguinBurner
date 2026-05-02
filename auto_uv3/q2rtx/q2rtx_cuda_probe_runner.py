"""Run one live Q2RTX/CUDA probe for an Auto-UV3 curve candidate.

It converts the raw workload result into the strict Auto-UV3 stability decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from stability.q2rtx import Q2RTXStabilityConfig

from ..auto_uv_types import AutoUvProbeSummary, VfCurveCandidate
from .probe_stability_decision import evaluate_stable_run
from .q2rtx_cuda_voltage_probe import probe_voltage_candidate
from .q2rtx_cuda_probe_config import (
    q2rtx_cuda_probe_config_for_voltage_band,
    short_q2rtx_probe_config,
)
from ..ui.ui_json_event_writer import AutoUvEventCallback
from ..ui.ui_voltage_probe_events import (
    emit_ui_voltage_probe_finished,
    emit_ui_voltage_probe_started,
)
from ..voltage_sweep_state import VoltageProbeOutcome


@dataclass(frozen=True, slots=True)
class Q2RtxCudaProbeRunner:
    reader: object
    live_voltage_reader: object
    q2rtx_config: Q2RTXStabilityConfig
    runtime_default_plan: list[dict]
    power_limit_w: int | None
    start_voltage_mv: int
    baseline_clock_mhz: float | None
    min_performance_core_clock_pct: float
    short_probe_base_duration_s: int
    timedemo_warmup_runs: int
    log: Callable[[str], None]
    event_callback: AutoUvEventCallback | None = None

    def probe_default_curve(
        self,
        *,
        base_curve: list[dict],
        label_voltage_mv: int,
        label_clock_mhz: int,
    ) -> tuple[AutoUvProbeSummary, object]:
        return probe_voltage_candidate(
            reader=self.reader,
            candidate_plan=base_curve,
            candidate_voltage_mv=int(label_voltage_mv),
            lock_clock_mhz=int(label_clock_mhz),
            q2rtx_config=short_q2rtx_probe_config(
                self.q2rtx_config,
                target_duration_s=int(self.short_probe_base_duration_s),
            ),
            stable_history=[],
            initial_probe_clock_mhz=None,
            nvml_session=self.live_voltage_reader,
            log=self.log,
            phase_label="discover",
            power_limit_w=self.power_limit_w,
            enforce_target_core_clock_floor=False,
            show_candidate_target=False,
            summarize_saturated_tail=True,
            use_power_limit_floor=True,
            reset_plan=self.runtime_default_plan,
            timedemo_warmup_runs=int(self.timedemo_warmup_runs),
            event_callback=self.event_callback,
        )

    def probe_baseline_candidate(
        self,
        candidate: VfCurveCandidate,
    ) -> VoltageProbeOutcome:
        return self.probe_candidate(
            candidate,
            stable_history=[],
            phase_label="baseline",
            enforce_target_core_clock_floor=False,
            summarize_saturated_tail=True,
            use_power_limit_floor=True,
        )

    def probe_sweep_candidate(
        self,
        candidate: VfCurveCandidate,
        *,
        stable_history: list[AutoUvProbeSummary],
        phase_label: str = "candidate",
    ) -> VoltageProbeOutcome:
        return self.probe_candidate(
            candidate,
            stable_history=stable_history,
            phase_label=phase_label,
            enforce_target_core_clock_floor=True,
            summarize_saturated_tail=False,
            use_power_limit_floor=False,
        )

    def probe_candidate(
        self,
        candidate: VfCurveCandidate,
        *,
        stable_history: list[AutoUvProbeSummary],
        phase_label: str,
        enforce_target_core_clock_floor: bool,
        summarize_saturated_tail: bool,
        use_power_limit_floor: bool,
    ) -> VoltageProbeOutcome:
        emit_ui_voltage_probe_started(
            self.event_callback,
            candidate,
            stage=str(phase_label),
            max_clock_drop_pct=self.max_clock_drop_pct,
        )
        config = q2rtx_cuda_probe_config_for_voltage_band(
            self.q2rtx_config,
            initial_target_voltage_mv=int(self.start_voltage_mv),
            candidate_voltage_mv=int(candidate.voltage_mv),
            base_duration_s=int(self.short_probe_base_duration_s),
        )
        summary, result = probe_voltage_candidate(
            reader=self.reader,
            candidate_plan=candidate.flattened_plan,
            candidate_voltage_mv=int(candidate.voltage_mv),
            lock_clock_mhz=int(candidate.target_mhz),
            q2rtx_config=config,
            stable_history=stable_history,
            initial_probe_clock_mhz=self.baseline_clock_mhz,
            nvml_session=self.live_voltage_reader,
            log=self.log,
            phase_label=str(phase_label),
            power_limit_w=self.power_limit_w,
            enforce_target_core_clock_floor=bool(enforce_target_core_clock_floor),
            summarize_saturated_tail=bool(summarize_saturated_tail),
            use_power_limit_floor=bool(use_power_limit_floor),
            min_performance_core_clock_pct=float(self.min_performance_core_clock_pct),
            reset_plan=self.runtime_default_plan,
            timedemo_warmup_runs=int(self.timedemo_warmup_runs),
            event_callback=self.event_callback,
        )
        outcome = self.outcome_from_probe_result(
            summary,
            result,
            stable_history=stable_history,
            q2rtx_config=config,
        )
        emit_ui_voltage_probe_finished(
            self.event_callback,
            candidate,
            outcome,
            stage=str(phase_label),
            max_clock_drop_pct=self.max_clock_drop_pct,
        )
        return outcome

    @property
    def max_clock_drop_pct(self) -> float:
        return max(0.0, 100.0 - float(self.min_performance_core_clock_pct))

    def outcome_from_probe_result(
        self,
        summary: AutoUvProbeSummary,
        result: object,
        *,
        stable_history: list[AutoUvProbeSummary],
        q2rtx_config: Q2RTXStabilityConfig | None = None,
    ) -> VoltageProbeOutcome:
        baseline_probe = stable_history[0] if stable_history else None
        effective_config = q2rtx_config or self.q2rtx_config
        cuda_required = bool(getattr(effective_config, "companion_command", None))
        decision = evaluate_stable_run(
            result,
            baseline_frames=(
                int(baseline_probe.frames_per_run)
                if baseline_probe is not None
                and baseline_probe.frames_per_run is not None
                else None
            ),
            baseline_fps=(
                float(baseline_probe.avg_fps)
                if baseline_probe is not None and baseline_probe.avg_fps is not None
                else None
            ),
            baseline_power_w=(
                float(baseline_probe.avg_power_w)
                if baseline_probe is not None
                and baseline_probe.avg_power_w is not None
                else None
            ),
            baseline_core_clock_mhz=(
                float(baseline_probe.avg_core_clock_mhz)
                if baseline_probe is not None
                and baseline_probe.avg_core_clock_mhz is not None
                else None
            ),
            power_limit_w=self.power_limit_w,
            cuda_required=cuda_required,
            companion_result={"success": True} if cuda_required else None,
            fatal_output_found=bool(getattr(result, "fatal_output_matches", [])),
            xid_found=bool(getattr(result, "xid_messages", [])),
        )
        return VoltageProbeOutcome(
            decision=decision,
            measured_core_clock_mhz=summary.avg_core_clock_mhz,
            measured_voltage_mv=summary.avg_voltage_mv,
            raw_probe=summary,
            raw_result=result,
        )
