from __future__ import annotations

import json

from saved_uv_profiles.profile_tiers import (
    PROFILE_TIER_BALANCED,
    PROFILE_TIER_EFFICIENCY,
    PROFILE_TIER_PERFORMANCE,
    available_adaptive_tiers,
    generated_profile_tier,
    generated_profile_tier_from_runtime_options,
    load_profile_tier_assignments,
    profile_tier_label,
    resolve_profile_tier_profiles,
    save_profile_tier_assignment,
)


def _profile(profile_id: str, tier: str, created_at: str) -> dict:
    return {
        "profile_id": profile_id,
        "candidate_id": profile_id,
        "profile_created_at": created_at,
        "final_verified": True,
        "generated_profile_tier": tier,
    }


def test_runtime_options_preserve_user_facing_balanced_tier() -> None:
    assert (
        generated_profile_tier_from_runtime_options(
            {"auto_uv_requested_mode": "balanced", "auto_uv_mode": "efficiency"}
        )
        == PROFILE_TIER_BALANCED
    )
    assert (
        generated_profile_tier_from_runtime_options({"auto_uv_mode": "performance"})
        == PROFILE_TIER_PERFORMANCE
    )
    assert generated_profile_tier_from_runtime_options({}) == PROFILE_TIER_BALANCED


def test_profile_tier_infers_legacy_balanced_from_tail_shape() -> None:
    assert generated_profile_tier({"auto_uv_mode": "efficiency", "tail_rise_bins": 4}) == PROFILE_TIER_BALANCED
    assert generated_profile_tier({"auto_uv_mode": "efficiency", "tail_rise_bins": 0}) == PROFILE_TIER_EFFICIENCY
    assert profile_tier_label("perf") == "Performance"


def test_profile_tier_assignment_is_one_tier_per_profile(tmp_path) -> None:
    path = tmp_path / "assignments.json"

    save_profile_tier_assignment("profile-a", "efficiency", path=path)
    assignments = save_profile_tier_assignment("profile-a", "performance", path=path)

    assert assignments == {PROFILE_TIER_PERFORMANCE: "profile-a"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["tiers"] == {"performance": "profile-a"}
    assert load_profile_tier_assignments(path) == {PROFILE_TIER_PERFORMANCE: "profile-a"}


def test_resolve_profile_tier_profiles_prefers_pinned_then_latest_generated() -> None:
    profiles = [
        _profile("old-eff", "Efficiency", "2026-06-01T12:00:00+00:00"),
        _profile("new-eff", "Efficiency", "2026-06-02T12:00:00+00:00"),
        _profile("bal", "Balanced", "2026-06-03T12:00:00+00:00"),
        _profile("perf", "Performance", "2026-06-04T12:00:00+00:00"),
    ]

    resolved = resolve_profile_tier_profiles(
        profiles,
        assignments={PROFILE_TIER_EFFICIENCY: "old-eff"},
    )

    assert resolved[PROFILE_TIER_EFFICIENCY]["profile_id"] == "old-eff"
    assert resolved[PROFILE_TIER_BALANCED]["profile_id"] == "bal"
    assert resolved[PROFILE_TIER_PERFORMANCE]["profile_id"] == "perf"
    assert available_adaptive_tiers(resolved) == [
        PROFILE_TIER_EFFICIENCY,
        PROFILE_TIER_BALANCED,
        PROFILE_TIER_PERFORMANCE,
    ]
