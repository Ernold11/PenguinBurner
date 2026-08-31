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


def test_the_new_knob_defaults_are_the_documented_wire_contract() -> None:
    """These literals must equal the daemon's serde defaults.

    The client omits any knob held at its default, so what a default config
    actually runs is the daemon's copy (burnerd/src/profile/runtime_spec.rs).
    Mirrored literal-for-literal by
    an_omitted_policy_tail_resolves_the_documented_defaults on the Rust side;
    a retune must change both, or default clients silently run the other
    language's number.
    """
    config = AdaptiveProfilePolicyConfig()

    assert (
        config.frame_cap_enter_gpu_pct,
        config.frame_cap_exit_gpu_pct,
        config.frame_cap_confirm_windows,
        config.frame_cap_exit_pacing_pct,
        config.desktop_idle_gpu_pct,
        config.desktop_idle_after_s,
    ) == (60.0, 90.0, 3, 15.0, 20.0, 60.0)


def test_frame_cap_enter_bar_matches_the_cpu_bound_one() -> None:
    """Both bars answer "is the GPU the limiter?", so they share one number.

    The frame-cap rule also steps DOWN, but the extra evidence that demands
    comes from frame_cap_confirm_windows (consecutive capped-looking
    readings), not from a stricter utilisation bar -- a stricter bar was what
    left the live-logged 51-58% capped games unrecognised.
    """
    config = AdaptiveProfilePolicyConfig()

    assert config.frame_cap_enter_gpu_pct == config.cpu_bound_gpu_util_max_pct
    assert config.frame_cap_confirm_windows > 1


def test_frame_cap_enter_bar_survives_target_scaling() -> None:
    """Scaling retargets the frametime thresholds, not the utilisation ones."""
    for fps in (30.0, 60.0, 100.0, 240.0):
        assert (
            AdaptiveProfilePolicyConfig.for_target_fps(fps).frame_cap_enter_gpu_pct
            == AdaptiveProfilePolicyConfig().frame_cap_enter_gpu_pct
        )


def test_frame_cap_enter_bar_honours_its_environment_override() -> None:
    config = AdaptiveProfilePolicyConfig().with_env_overrides(
        {"PENGUIN_BURNER_ADAPTIVE_FRAME_CAP_ENTER_GPU_PCT": "25"}
    )

    assert config.frame_cap_enter_gpu_pct == 25.0


def test_the_three_utilisation_bars_are_ordered_idle_enter_exit() -> None:
    """Idle < cap enter < cap exit, and the daemon rejects any other order.

    Each bar answers a different question about the same reading, so the
    defaults have to keep them apart: doing nothing, working under a limit,
    and flat out.
    """
    config = AdaptiveProfilePolicyConfig()

    assert config.desktop_idle_gpu_pct < config.frame_cap_enter_gpu_pct
    assert config.frame_cap_enter_gpu_pct < config.frame_cap_exit_gpu_pct


def test_the_exit_and_idle_bars_honour_their_environment_overrides() -> None:
    config = AdaptiveProfilePolicyConfig().with_env_overrides(
        {
            "PENGUIN_BURNER_ADAPTIVE_FRAME_CAP_EXIT_GPU_PCT": "85",
            "PENGUIN_BURNER_ADAPTIVE_DESKTOP_IDLE_GPU_PCT": "12",
        }
    )

    assert config.frame_cap_exit_gpu_pct == 85.0
    assert config.desktop_idle_gpu_pct == 12.0


def test_the_confirm_pacing_and_idle_delay_knobs_honour_their_envs() -> None:
    config = AdaptiveProfilePolicyConfig().with_env_overrides(
        {
            "PENGUIN_BURNER_ADAPTIVE_FRAME_CAP_CONFIRM_WINDOWS": "5",
            "PENGUIN_BURNER_ADAPTIVE_FRAME_CAP_EXIT_PACING_PCT": "25",
            "PENGUIN_BURNER_ADAPTIVE_DESKTOP_IDLE_AFTER_S": "120",
        }
    )

    assert config.frame_cap_confirm_windows == 5
    assert config.frame_cap_exit_pacing_pct == 25.0
    assert config.desktop_idle_after_s == 120.0


def test_the_exit_and_idle_bars_survive_target_scaling() -> None:
    """Scaling retargets the frametime thresholds, not the utilisation ones."""
    default = AdaptiveProfilePolicyConfig()
    for fps in (30.0, 60.0, 100.0, 240.0):
        scaled = AdaptiveProfilePolicyConfig.for_target_fps(fps)
        assert scaled.frame_cap_exit_gpu_pct == default.frame_cap_exit_gpu_pct
        assert scaled.frame_cap_confirm_windows == default.frame_cap_confirm_windows
        assert scaled.frame_cap_exit_pacing_pct == default.frame_cap_exit_pacing_pct
        assert scaled.desktop_idle_gpu_pct == default.desktop_idle_gpu_pct
        assert scaled.desktop_idle_after_s == default.desktop_idle_after_s
