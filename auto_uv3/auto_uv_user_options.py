"""Keep Auto-UV user-facing numeric options in one readable place.

These values are CLI/UI knobs; rule modules read them but do not mutate them.
"""

from __future__ import annotations

from dataclasses import dataclass


AUTO_UV_MAX_CLOCK_BUMP_BUDGET_RATIO = 1.5
AUTO_UV_YOLO_MAX_CLOCK_BUMP_BUDGET_RATIO = 1.75


@dataclass(frozen=True, slots=True)
class AutoUvDefaults:
    probe_duration_s: int = 20
    shape_probe_duration_s: int = 10
    final_duration_s: int = 600
    max_drop_pct: float = 16.0
    performance_max_drop_pct: float = 14.0
    efficiency_stop_streak: int = 1
    clock_bump_budget_ratio: float = 0.75
    performance_clock_bump_budget_ratio: float = 1.10


@dataclass(frozen=True, slots=True)
class AutoUvCurveTuning:
    aggressive_max_step_bins: int = 3
    aggressive_min_step_bins: int = 2
    fine_step_bins: int = 1
    upper_voltage_nominal_drop_mv: int = 15
    upper_voltage_effective_gap_mv: float = 20.0
    lower_voltage_nominal_drop_mv: int = 5
    lower_voltage_effective_gap_mv: float = 5.0
    clock_step_mhz: int = 15
    clock_select_tolerance_mhz: float = 5.0
    flatten_ramp_window_mv: int = 40


@dataclass(frozen=True, slots=True)
class AutoUvMetricTuning:
    min_temp_normalized_fps_per_w_improvement_pct: float = 0.5
    min_efficiency_stop_voltage_drop_pct: float = 10.0
    temperature_normalization_power_pct_per_c: float = 0.5
    temperature_normalization_max_delta_c: float = 10.0
    loaded_sample_warmup_s: float = 5.0
    performance_timedemo_warmup_runs: int = 1
    timedemo_warmup_min_remaining_runs: int = 3
    saturated_tail_power_pct: float = 90.0
    saturated_tail_core_clock_pct: float = 98.0
    saturated_tail_min_samples: int = 2
    min_performance_core_clock_pct: float = 90.0
    min_proper_run_fps_pct: float = 90.0
    min_proper_run_fps_regression_streak: int = 2
    min_proper_run_power_pct: float = 50.0
    target_core_clock_low_streak_samples: int = 3
    power_saturation_headroom_pct: float = 2.0
    loaded_sample_power_floor_pct: float = 75.0
    active_core_clock_percentile: float = 0.75
    loaded_voltage_floor_percentile: float = 0.10
    loaded_voltage_ceiling_percentile: float = 0.90

    @property
    def max_core_clock_drop_pct(self) -> float:
        return max(0.0, 100.0 - float(self.min_performance_core_clock_pct))


@dataclass(frozen=True, slots=True)
class AutoUvProbeTuning:
    tiered_cuda_duration_s: int = 15
    timeout_multiplier: float = 2.0
    short_timeout_buffer_s: float = 15.0
    long_timeout_buffer_s: float = 60.0
    high_voltage_pct: float = 95.0
    medium_voltage_pct: float = 90.0


@dataclass(frozen=True, slots=True)
class AutoUvStallTuning:
    timeout_min_s: float = 15.0
    timeout_multiplier: float = 2.5
    live_core_clock_abort_min_samples: int = 8
    avg_core_clock_abort_min_samples: int = 12
    busy_gpu_util_pct: float = 60.0
    busy_reference_power_pct: float = 50.0
    busy_power_limit_pct: float = 35.0


@dataclass(frozen=True, slots=True)
class AutoUvFanTuning:
    max_base_curve_load_temp_c: float = 75.0
    cooling_headroom_speed_reduction_pct_per_c: float = 1.5
    cooling_headroom_max_speed_reduction_pct: float = 15.0
    cooling_headroom_exponential_power_per_c: float = 0.15
    cooling_headroom_max_exponential_power_bonus: float = 2.0
    max_curve_points: int = 10
    zero_rpm_until_temp_c: float = 45.0
    minimum_active_temp_c: float = 60.0
    minimum_active_speed_pct: float = 30.0
    exponential_points: int = 5
    exponential_power: float = 2.4
    load_anchor_max_speed_pct: float = 65.0
    emergency_temp_c: float = 80.0
    emergency_min_speed_pct: float = 75.0
    full_speed_temp_c: float = 90.0
    full_speed_pct: float = 100.0
    hardware_auto_override_temp_c: float = 95.0
    emergency_resume_temp_c: float = 80.0
    fallback_speed_pct: float = 45.0
    poll_interval_s: int = 2
    hysteresis_c: float = 2.0
    min_speed_pct: int = 0
    max_speed_pct: int = 100
    max_step_up_pct_per_s: float = 25.0
    max_step_down_pct_per_s: float = 15.0


AUTO_UV_DEFAULTS = AutoUvDefaults()
AUTO_UV_CURVE_TUNING = AutoUvCurveTuning()
AUTO_UV_FAN_TUNING = AutoUvFanTuning()
AUTO_UV_METRIC_TUNING = AutoUvMetricTuning()
AUTO_UV_PROBE_TUNING = AutoUvProbeTuning()
AUTO_UV_STALL_TUNING = AutoUvStallTuning()
