from .models import (
    AutoUvCurveCandidate,
    AutoUvError,
    AutoUvFinalChoiceDiscarded,
    AutoUvProbeSummary,
    AutoUvVoltageScanResult,
    VoltageCurve,
    VoltagePoint,
)
from .afterburner_defaults import restore_afterburner_defaults_from_config
from .probe_config import build_long_stability_test_config, long_stability_workload_durations
from .scan import run_auto_uv_voltage_scan
from .tuning import AUTO_UV_DEFAULTS
from .candidate_decision import (
    AutoUv2CandidateChoice,
    AutoUv2OverclockBudget,
    AutoUv2SweepState,
    choose_next_candidate,
    predict_clock_floor_miss,
)
from .overclock_recovery import AutoUv2OverclockAttempt, make_overclock_attempt
from .probe_decision import AutoUv2ProbeDecision, classify_probe_result
from .sweep_state import AutoUv2SweepUpdate, apply_probe_decision
from .stop_decision import AutoUv2EfficiencyStop, decide_efficiency_stop
from .sweep import AutoUv2SweepEvent, AutoUv2SweepHooks, AutoUv2SweepResult, run_sweep
from .sweep_modes import (
    AUTO_UV_MODE_EFFICIENCY,
    AUTO_UV_MODE_PERFORMANCE,
    AUTO_UV_MODES,
    normalize_auto_uv_mode,
)

DEFAULT_AUTO_UV_DURATION_S = AUTO_UV_DEFAULTS.probe_duration_s
DEFAULT_AUTO_UV_FINAL_DURATION_S = AUTO_UV_DEFAULTS.final_duration_s

__all__ = [
    "AutoUvCurveCandidate",
    "AutoUvError",
    "AutoUvFinalChoiceDiscarded",
    "AutoUv2CandidateChoice",
    "AutoUv2EfficiencyStop",
    "AutoUv2OverclockAttempt",
    "AutoUv2OverclockBudget",
    "AutoUv2ProbeDecision",
    "AutoUv2SweepEvent",
    "AutoUv2SweepHooks",
    "AutoUv2SweepResult",
    "AutoUv2SweepState",
    "AutoUv2SweepUpdate",
    "AutoUvProbeSummary",
    "AutoUvVoltageScanResult",
    "AUTO_UV_MODE_EFFICIENCY",
    "AUTO_UV_MODE_PERFORMANCE",
    "AUTO_UV_MODES",
    "DEFAULT_AUTO_UV_DURATION_S",
    "DEFAULT_AUTO_UV_FINAL_DURATION_S",
    "VoltageCurve",
    "VoltagePoint",
    "apply_probe_decision",
    "build_long_stability_test_config",
    "long_stability_workload_durations",
    "choose_next_candidate",
    "classify_probe_result",
    "decide_efficiency_stop",
    "make_overclock_attempt",
    "normalize_auto_uv_mode",
    "predict_clock_floor_miss",
    "restore_afterburner_defaults_from_config",
    "run_sweep",
    "run_auto_uv_voltage_scan",
]
