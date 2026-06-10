from __future__ import annotations

from runtime_gpu_control.adaptive_profile_policy import AdaptiveProfileController
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


def test_adaptive_policy_allows_two_tier_operation_when_middle_missing() -> None:
    controller = AdaptiveProfileController(initial_tier=PROFILE_TIER_EFFICIENCY)

    decision = controller.update(
        present_frametime_p95_ms=24.0,
        available_tiers=[PROFILE_TIER_EFFICIENCY, PROFILE_TIER_PERFORMANCE],
        now_monotonic=10.0,
    )

    assert decision.changed is True
    assert decision.tier == PROFILE_TIER_PERFORMANCE
