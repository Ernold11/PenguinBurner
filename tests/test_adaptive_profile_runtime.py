from __future__ import annotations

from runtime_gpu_control.adaptive_profile_runtime import (
    AdaptiveAutoUvRuntimeController,
    AdaptiveAutoUvRuntimeDependencies,
)
from saved_uv_profiles.profile_tiers import (
    PROFILE_TIER_EFFICIENCY,
    PROFILE_TIER_PERFORMANCE,
)


class FakeReader:
    def __init__(self) -> None:
        self.refresh_count = 0

    def refresh_points(self) -> None:
        self.refresh_count += 1


class FakeClockCeiling:
    def __init__(self) -> None:
        self.targets = []

    def retarget(self, flatten_target) -> None:
        self.targets.append(flatten_target)


class FakeOverlayPublisher:
    profile_tier = ""
    profile_tier_key = ""
    profile_id = ""


def _curve(profile_id: str, tier: str, plan: list[dict]) -> dict:
    return {
        "profile_id": profile_id,
        "profile_tier": tier.title(),
        "profile_tier_key": tier,
        "path": f"/tmp/{profile_id}.json",
        "plan": plan,
        "memory_offset_mhz": 500,
        "flatten_target": {
            "lock_clock_mhz": 2700,
            "lock_voltage_mv": 875,
        },
    }


def test_adaptive_runtime_switch_applies_target_tier_profile() -> None:
    reader = FakeReader()
    clock_ceiling = FakeClockCeiling()
    overlay = FakeOverlayPublisher()
    applied = []
    memory_offsets = []
    profiles = [
        {"profile_id": "eff", "final_verified": True},
        {"profile_id": "perf", "final_verified": True},
    ]
    curves = {
        "eff": _curve("eff", PROFILE_TIER_EFFICIENCY, [{"index": 1}]),
        "perf": _curve("perf", PROFILE_TIER_PERFORMANCE, [{"index": 2}]),
    }
    deps = AdaptiveAutoUvRuntimeDependencies(
        read_auto_uv_profiles=lambda: profiles,
        resolve_profile_tier_profiles=lambda _profiles: {
            PROFILE_TIER_EFFICIENCY: profiles[0],
            PROFILE_TIER_PERFORMANCE: profiles[1],
        },
        load_auto_uv_final_curve=lambda profile_id: curves[profile_id],
        apply_plan=lambda target_reader, plan: applied.append((target_reader, plan)),
        apply_memory_offset=lambda **kwargs: (
            memory_offsets.append(kwargs) or {"mem_clk_vf_offset_mhz": 500}
        ),
        select_expected_vf_samples=lambda plan: [("expected", plan)],
        log=lambda _message: None,
    )
    controller = AdaptiveAutoUvRuntimeController(
        current_tier=PROFILE_TIER_EFFICIENCY,
        vf_curve_reader=reader,
        gpu_policy_controller=object(),
        clock_ceiling_controller=clock_ceiling,
        overlay_state_publisher=overlay,
        dependencies=deps,
    )

    result = controller.update(
        latency_snapshot={"present_frametime_p95_ms": 24.0},
        now_monotonic=10.0,
    )

    assert result is not None
    assert result.changed is True
    assert result.tier == PROFILE_TIER_PERFORMANCE
    assert applied == [(reader, curves["perf"]["plan"])]
    assert reader.refresh_count == 1
    assert clock_ceiling.targets == [curves["perf"]["flatten_target"]]
    assert memory_offsets[0]["memory_offset_mhz"] == 500
    assert result.vf_expected_samples == [("expected", curves["perf"]["plan"])]
    assert overlay.profile_tier == "Performance"
    assert overlay.profile_tier_key == PROFILE_TIER_PERFORMANCE
    assert overlay.profile_id == "perf"
