from __future__ import annotations

from runtime.gpu_control.adaptive_profile_policy import AdaptiveProfilePolicyConfig


def test_adaptive_policy_reads_wait_env_overrides() -> None:
    config = AdaptiveProfilePolicyConfig.for_target_fps(
        60,
        env={
            "PENGUIN_BURNER_ADAPTIVE_TARGET_SLOW_WINDOWS": "2",
            "PENGUIN_BURNER_ADAPTIVE_NEAR_SLOW_WINDOWS": "1",
            "PENGUIN_BURNER_ADAPTIVE_COMFORT_WINDOWS": "4",
            "PENGUIN_BURNER_ADAPTIVE_PERFORMANCE_COMFORT_WINDOWS": "5",
            "PENGUIN_BURNER_ADAPTIVE_DEMOTE_DWELL_S": "30",
            "PENGUIN_BURNER_ADAPTIVE_PERFORMANCE_DEMOTE_DWELL_S": "20",
            "PENGUIN_BURNER_ADAPTIVE_CPU_BOUND_GPU_UTIL_MAX": "80",
            "PENGUIN_BURNER_ADAPTIVE_CPU_BOUND_PEAK_THREAD_MIN": "65",
            "PENGUIN_BURNER_ADAPTIVE_CPU_BOUND_PROCESS_UTIL_MIN": "10",
        },
    )

    assert config.target_slow_windows == 2
    assert config.near_slow_windows == 1
    assert config.comfort_windows == 4
    assert config.performance_comfort_windows == 5
    assert config.demote_dwell_s == 30.0
    assert config.performance_demote_dwell_s == 20.0
    assert config.cpu_bound_gpu_util_max_pct == 80.0
    assert config.cpu_bound_peak_thread_min_pct == 65.0
    assert config.cpu_bound_process_util_min_pct == 10.0


def test_adaptive_policy_scales_thresholds_from_target_fps() -> None:
    config = AdaptiveProfilePolicyConfig.for_target_fps(30, env={})

    assert config.target_ms == 1000.0 / 30.0
    assert config.comfort_ms == 14.5 * (config.target_ms / 16.6)
    assert config.near_slow_ms == 18.5 * (config.target_ms / 16.6)
    assert config.badly_slow_ms == 22.0 * (config.target_ms / 16.6)


def test_adaptive_policy_rejects_out_of_range_overrides() -> None:
    default = AdaptiveProfilePolicyConfig()
    config = default.with_env_overrides(
        {
            "PENGUIN_BURNER_ADAPTIVE_TARGET_SLOW_WINDOWS": "0",
            "PENGUIN_BURNER_ADAPTIVE_NEAR_SLOW_WINDOWS": "121",
            "PENGUIN_BURNER_ADAPTIVE_DEMOTE_DWELL_S": "-1",
            "PENGUIN_BURNER_ADAPTIVE_CPU_BOUND_GPU_UTIL_MAX": "101",
        }
    )

    assert config.target_slow_windows == default.target_slow_windows
    assert config.near_slow_windows == default.near_slow_windows
    assert config.demote_dwell_s == default.demote_dwell_s
    assert config.cpu_bound_gpu_util_max_pct == default.cpu_bound_gpu_util_max_pct
