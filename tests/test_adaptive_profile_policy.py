from __future__ import annotations

from runtime_gpu_control.adaptive_profile_policy import (
    AdaptiveProfileController,
    AdaptiveProfilePolicyConfig,
)
from saved_uv_profiles.profile_tiers import (
    PROFILE_TIER_BALANCED,
    PROFILE_TIER_EFFICIENCY,
    PROFILE_TIER_PERFORMANCE,
)


def test_adaptive_policy_jumps_to_performance_when_badly_slow() -> None:
    controller = AdaptiveProfileController(initial_tier=PROFILE_TIER_EFFICIENCY)

    decision = controller.update(
        present_frametime_p95_ms=24.0,
        available_tiers=[
            PROFILE_TIER_EFFICIENCY,
            PROFILE_TIER_BALANCED,
            PROFILE_TIER_PERFORMANCE,
        ],
        now_monotonic=10.0,
    )

    assert decision.changed is True
    assert decision.tier == PROFILE_TIER_PERFORMANCE
    assert decision.reason == "badly-slow"


def test_adaptive_policy_near_slow_promotes_after_consecutive_windows() -> None:
    controller = AdaptiveProfileController(initial_tier=PROFILE_TIER_EFFICIENCY)
    tiers = [PROFILE_TIER_EFFICIENCY, PROFILE_TIER_BALANCED, PROFILE_TIER_PERFORMANCE]

    first = controller.update(
        present_frametime_p95_ms=17.2,
        available_tiers=tiers,
        now_monotonic=10.0,
    )
    second = controller.update(
        present_frametime_p95_ms=17.4,
        available_tiers=tiers,
        now_monotonic=20.0,
    )
    third = controller.update(
        present_frametime_p95_ms=17.3,
        available_tiers=tiers,
        now_monotonic=30.0,
    )

    assert first.changed is False
    assert second.changed is False
    assert third.changed is True
    assert third.tier == PROFILE_TIER_BALANCED


def test_adaptive_policy_demotes_only_after_comfort_and_dwell() -> None:
    controller = AdaptiveProfileController(initial_tier=PROFILE_TIER_BALANCED)
    tiers = [PROFILE_TIER_EFFICIENCY, PROFILE_TIER_BALANCED]

    for index in range(5):
        decision = controller.update(
            present_frametime_p95_ms=13.8,
            available_tiers=tiers,
            now_monotonic=10.0 + index,
        )
        assert decision.changed is False

    early = controller.update(
        present_frametime_p95_ms=13.7,
        available_tiers=tiers,
        now_monotonic=40.0,
    )
    late = controller.update(
        present_frametime_p95_ms=13.7,
        available_tiers=tiers,
        now_monotonic=70.0,
    )

    assert early.changed is False
    assert late.changed is True
    assert late.tier == PROFILE_TIER_EFFICIENCY


def test_adaptive_policy_performance_demote_dwell_default_is_45s() -> None:
    controller = AdaptiveProfileController(initial_tier=PROFILE_TIER_PERFORMANCE)
    tiers = [PROFILE_TIER_EFFICIENCY, PROFILE_TIER_PERFORMANCE]

    for index in range(9):
        decision = controller.update(
            present_frametime_p95_ms=11.8,
            available_tiers=tiers,
            now_monotonic=10.0 + index,
        )
        assert decision.changed is False

    early = controller.update(
        present_frametime_p95_ms=11.8,
        available_tiers=tiers,
        now_monotonic=44.0,
    )
    late = controller.update(
        present_frametime_p95_ms=11.8,
        available_tiers=tiers,
        now_monotonic=45.0,
    )

    assert early.changed is False
    assert late.changed is True
    assert late.tier == PROFILE_TIER_EFFICIENCY


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


def test_adaptive_policy_allows_two_tier_operation_when_middle_missing() -> None:
    controller = AdaptiveProfileController(initial_tier=PROFILE_TIER_EFFICIENCY)

    decision = controller.update(
        present_frametime_p95_ms=24.0,
        available_tiers=[PROFILE_TIER_EFFICIENCY, PROFILE_TIER_PERFORMANCE],
        now_monotonic=10.0,
    )

    assert decision.changed is True
    assert decision.tier == PROFILE_TIER_PERFORMANCE


def test_adaptive_policy_scales_thresholds_from_target_fps() -> None:
    config = AdaptiveProfilePolicyConfig.for_target_fps(30)
    controller = AdaptiveProfileController(
        initial_tier=PROFILE_TIER_EFFICIENCY,
        config=config,
    )
    tiers = [PROFILE_TIER_EFFICIENCY, PROFILE_TIER_PERFORMANCE]

    ok = controller.update(
        present_frametime_p95_ms=25.0,
        available_tiers=tiers,
        now_monotonic=10.0,
    )

    assert ok.changed is False
    assert ok.reason == "comfort-wait"
    assert config.target_ms == 1000.0 / 30.0

    first = controller.update(
        present_frametime_p95_ms=35.0,
        available_tiers=tiers,
        now_monotonic=20.0,
    )
    second = controller.update(
        present_frametime_p95_ms=35.0,
        available_tiers=tiers,
        now_monotonic=30.0,
    )
    third = controller.update(
        present_frametime_p95_ms=35.0,
        available_tiers=tiers,
        now_monotonic=40.0,
    )

    assert first.changed is False
    assert second.changed is False
    assert third.changed is True
    assert third.tier == PROFILE_TIER_PERFORMANCE


def test_adaptive_policy_caps_badly_slow_cpu_bound_jump_at_balanced() -> None:
    controller = AdaptiveProfileController(initial_tier=PROFILE_TIER_EFFICIENCY)

    decision = controller.update(
        present_frametime_p95_ms=24.0,
        available_tiers=[
            PROFILE_TIER_EFFICIENCY,
            PROFILE_TIER_BALANCED,
            PROFILE_TIER_PERFORMANCE,
        ],
        now_monotonic=10.0,
        gpu_util_pct=85.0,
        cpu_peak_thread_pct=74.0,
    )

    assert decision.changed is True
    assert decision.tier == PROFILE_TIER_BALANCED
    assert decision.reason == "cpu-bound-performance-cap"


def test_adaptive_policy_blocks_performance_when_cpu_bound() -> None:
    controller = AdaptiveProfileController(initial_tier=PROFILE_TIER_BALANCED)

    decision = controller.update(
        present_frametime_p95_ms=24.0,
        available_tiers=[
            PROFILE_TIER_EFFICIENCY,
            PROFILE_TIER_BALANCED,
            PROFILE_TIER_PERFORMANCE,
        ],
        now_monotonic=10.0,
        gpu_util_pct=85.0,
        cpu_peak_thread_pct=74.0,
    )

    assert decision.changed is False
    assert decision.tier == PROFILE_TIER_BALANCED
    assert decision.reason == "cpu-bound-performance-block"


def test_adaptive_policy_allows_performance_when_gpu_is_busy() -> None:
    controller = AdaptiveProfileController(initial_tier=PROFILE_TIER_BALANCED)

    decision = controller.update(
        present_frametime_p95_ms=24.0,
        available_tiers=[
            PROFILE_TIER_EFFICIENCY,
            PROFILE_TIER_BALANCED,
            PROFILE_TIER_PERFORMANCE,
        ],
        now_monotonic=10.0,
        gpu_util_pct=96.0,
        cpu_peak_thread_pct=98.0,
    )

    assert decision.changed is True
    assert decision.tier == PROFILE_TIER_PERFORMANCE
    assert decision.reason == "badly-slow"


def test_adaptive_policy_process_cpu_pressure_can_block_performance() -> None:
    controller = AdaptiveProfileController(initial_tier=PROFILE_TIER_BALANCED)

    decision = controller.update(
        present_frametime_p95_ms=24.0,
        available_tiers=[
            PROFILE_TIER_EFFICIENCY,
            PROFILE_TIER_BALANCED,
            PROFILE_TIER_PERFORMANCE,
        ],
        now_monotonic=10.0,
        gpu_util_pct=85.0,
        cpu_util_pct=18.0,
        cpu_peak_thread_pct=55.0,
    )

    assert decision.changed is False
    assert decision.tier == PROFILE_TIER_BALANCED
    assert decision.reason == "cpu-bound-performance-block"
