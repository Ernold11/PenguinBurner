from __future__ import annotations

import pytest

from runtime.support import adaptive_target_fps
from runtime.gpu_control.adaptive_profile_runtime import (
    AdaptiveAutoUvRuntimeController,
    AdaptiveAutoUvRuntimeDependencies,
)
from profiles.uv.profile_tiers import (
    PROFILE_TIER_EFFICIENCY,
    PROFILE_TIER_PERFORMANCE,
)


@pytest.fixture(autouse=True)
def _isolate_adaptive_target_fps(tmp_path, monkeypatch):
    """Keep policy thresholds deterministic across machines.

    The controller resolves its initial target FPS via
    adaptive_target_fps_from_env(), which falls back to the real user runtime
    config. Without isolation these tests read whatever Target FPS the developer
    set in the UI (e.g. 30), shifting the frametime thresholds and breaking the
    assumed default (60 FPS) behavior. Point the config lookup at an empty temp
    file and clear any env override so the built-in default applies.
    """
    monkeypatch.setattr(
        adaptive_target_fps,
        "default_runtime_config_path",
        lambda: tmp_path / "penguin_burner.toml",
    )
    for name in adaptive_target_fps.ADAPTIVE_TARGET_FPS_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


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
    last_gpu_util_pct = None
    last_cpu_util_pct = None
    last_cpu_peak_thread_pct = None


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
        latency_snapshot={"base_present_frametime_p95_ms": 24.0},
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


def test_adaptive_runtime_switch_applies_profile_power_limit() -> None:
    reader = FakeReader()
    applied = []
    power_limits = []
    profiles = [
        {"profile_id": "eff", "final_verified": True},
        {"profile_id": "perf", "final_verified": True},
    ]
    curves = {
        "eff": _curve("eff", PROFILE_TIER_EFFICIENCY, [{"index": 1}]),
        "perf": {
            **_curve("perf", PROFILE_TIER_PERFORMANCE, [{"index": 2}]),
            "power_limit_w": 390,
        },
    }
    deps = AdaptiveAutoUvRuntimeDependencies(
        read_auto_uv_profiles=lambda: profiles,
        resolve_profile_tier_profiles=lambda _profiles: {
            PROFILE_TIER_EFFICIENCY: profiles[0],
            PROFILE_TIER_PERFORMANCE: profiles[1],
        },
        load_auto_uv_final_curve=lambda profile_id: curves[profile_id],
        apply_plan=lambda target_reader, plan: applied.append((target_reader, plan)),
        apply_power_limit=lambda **kwargs: (
            power_limits.append(kwargs) or {"power_limit_w": kwargs["power_limit_w"]}
        ),
        apply_memory_offset=lambda **_kwargs: {},
        select_expected_vf_samples=lambda _plan: [],
        log=lambda _message: None,
    )
    policy_controller = object()
    controller = AdaptiveAutoUvRuntimeController(
        current_tier=PROFILE_TIER_EFFICIENCY,
        vf_curve_reader=reader,
        gpu_policy_controller=policy_controller,
        dependencies=deps,
    )

    result = controller.update(
        latency_snapshot={"base_present_frametime_p95_ms": 24.0},
        now_monotonic=10.0,
    )

    assert result is not None
    assert result.power_limit_w == 390
    assert power_limits == [
        {
            "profile_label": "adaptive Performance profile",
            "power_limit_w": 390,
            "gpu_policy_controller": policy_controller,
        }
    ]


def test_adaptive_runtime_ignores_raw_framegen_present_pacing() -> None:
    reader = FakeReader()
    applied = []
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
        apply_memory_offset=lambda **_kwargs: {},
        select_expected_vf_samples=lambda _plan: [],
        log=lambda _message: None,
    )
    controller = AdaptiveAutoUvRuntimeController(
        current_tier=PROFILE_TIER_EFFICIENCY,
        vf_curve_reader=reader,
        gpu_policy_controller=object(),
        dependencies=deps,
    )

    result = controller.update(
        latency_snapshot={
            "present_frametime_p95_ms": 8.333,
            "present_fps": "n/a",
            "raw_present_fps_stats": {"avg": "120"},
        },
        now_monotonic=10.0,
    )

    assert result is not None
    assert result.changed is False
    assert result.reason == "no-sample"
    assert applied == []


def test_adaptive_runtime_prefers_base_present_frametime() -> None:
    reader = FakeReader()
    applied = []
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
        apply_memory_offset=lambda **_kwargs: {},
        select_expected_vf_samples=lambda _plan: [],
        log=lambda _message: None,
    )
    controller = AdaptiveAutoUvRuntimeController(
        current_tier=PROFILE_TIER_EFFICIENCY,
        vf_curve_reader=reader,
        gpu_policy_controller=object(),
        dependencies=deps,
    )

    result = controller.update(
        latency_snapshot={
            "present_frametime_p95_ms": 8.333,
            "base_present_frametime_p95_ms": 25.0,
            "present_fps": "40",
            "raw_present_fps_stats": {"avg": "120"},
        },
        now_monotonic=10.0,
    )

    assert result is not None
    assert result.changed is True
    assert result.tier == PROFILE_TIER_PERFORMANCE
    assert applied == [(reader, curves["perf"]["plan"])]


def test_adaptive_runtime_reads_target_fps_from_env(monkeypatch) -> None:
    monkeypatch.setenv("PENGUIN_BURNER_ADAPTIVE_TARGET_FPS", "30")
    reader = FakeReader()
    applied = []
    profiles = [
        {"profile_id": "eff", "final_verified": True},
        {"profile_id": "perf", "final_verified": True},
    ]
    curves = {
        "eff": _curve("eff", PROFILE_TIER_EFFICIENCY, [{"index": 1}]),
        "perf": _curve("perf", PROFILE_TIER_PERFORMANCE, [{"index": 2}]),
    }
    logs = []
    deps = AdaptiveAutoUvRuntimeDependencies(
        read_auto_uv_profiles=lambda: profiles,
        resolve_profile_tier_profiles=lambda _profiles: {
            PROFILE_TIER_EFFICIENCY: profiles[0],
            PROFILE_TIER_PERFORMANCE: profiles[1],
        },
        load_auto_uv_final_curve=lambda profile_id: curves[profile_id],
        apply_plan=lambda target_reader, plan: applied.append((target_reader, plan)),
        apply_memory_offset=lambda **_kwargs: {},
        select_expected_vf_samples=lambda _plan: [],
        log=logs.append,
    )
    controller = AdaptiveAutoUvRuntimeController(
        current_tier=PROFILE_TIER_EFFICIENCY,
        vf_curve_reader=reader,
        gpu_policy_controller=object(),
        dependencies=deps,
    )

    result = controller.update(
        latency_snapshot={"base_present_frametime_p95_ms": 25.0},
        now_monotonic=10.0,
    )

    assert result is not None
    assert result.changed is False
    assert applied == []
    assert any("30 FPS (33.33 ms p95)" in message for message in logs)


def test_adaptive_runtime_refreshes_target_fps_from_runtime_config(tmp_path) -> None:
    reader = FakeReader()
    applied = []
    logs = []
    runtime_config = tmp_path / "penguin_burner.toml"
    runtime_config.write_text("[adaptive]\ntarget_fps = 60\n", encoding="utf-8")
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
        apply_memory_offset=lambda **_kwargs: {},
        select_expected_vf_samples=lambda _plan: [],
        log=logs.append,
    )
    controller = AdaptiveAutoUvRuntimeController(
        current_tier=PROFILE_TIER_EFFICIENCY,
        vf_curve_reader=reader,
        gpu_policy_controller=object(),
        adaptive_target_fps=30,
        runtime_config_path=runtime_config,
        dependencies=deps,
    )

    result = controller.update(
        latency_snapshot={"base_present_frametime_p95_ms": 24.0},
        now_monotonic=10.0,
    )

    assert result is not None
    assert result.changed is True
    assert result.tier == PROFILE_TIER_PERFORMANCE
    assert applied == [(reader, curves["perf"]["plan"])]
    assert any("target updated: 60 FPS (16.67 ms p95)" in message for message in logs)


def test_adaptive_runtime_restores_policy_state_after_apply_failure() -> None:
    reader = FakeReader()
    apply_attempts = []
    logs = []
    profiles = [
        {"profile_id": "eff", "final_verified": True},
        {"profile_id": "perf", "final_verified": True},
    ]
    curves = {
        "eff": _curve("eff", PROFILE_TIER_EFFICIENCY, [{"index": 1}]),
        "perf": _curve("perf", PROFILE_TIER_PERFORMANCE, [{"index": 2}]),
    }

    def fail_apply(target_reader, plan) -> None:
        apply_attempts.append((target_reader, plan))
        raise RuntimeError("driver rejected curve")

    deps = AdaptiveAutoUvRuntimeDependencies(
        read_auto_uv_profiles=lambda: profiles,
        resolve_profile_tier_profiles=lambda _profiles: {
            PROFILE_TIER_EFFICIENCY: profiles[0],
            PROFILE_TIER_PERFORMANCE: profiles[1],
        },
        load_auto_uv_final_curve=lambda profile_id: curves[profile_id],
        apply_plan=fail_apply,
        apply_memory_offset=lambda **_kwargs: {},
        select_expected_vf_samples=lambda _plan: [],
        log=logs.append,
    )
    controller = AdaptiveAutoUvRuntimeController(
        current_tier=PROFILE_TIER_EFFICIENCY,
        vf_curve_reader=reader,
        gpu_policy_controller=object(),
        dependencies=deps,
    )

    first = controller.update(
        latency_snapshot={"base_present_frametime_p95_ms": 24.0},
        now_monotonic=10.0,
    )
    second = controller.update(
        latency_snapshot={"base_present_frametime_p95_ms": 24.0},
        now_monotonic=20.0,
    )

    assert first is not None
    assert first.changed is False
    assert first.tier == PROFILE_TIER_EFFICIENCY
    assert first.reason == "apply-failed"
    assert controller.policy.current_tier == PROFILE_TIER_EFFICIENCY
    assert second is not None
    assert second.reason == "apply-failed"
    assert apply_attempts == [
        (reader, curves["perf"]["plan"]),
        (reader, curves["perf"]["plan"]),
    ]
    assert any("driver rejected curve" in message for message in logs)


def test_adaptive_runtime_passes_overlay_busy_metrics_to_policy() -> None:
    reader = FakeReader()
    overlay = FakeOverlayPublisher()
    overlay.last_gpu_util_pct = 85
    overlay.last_cpu_peak_thread_pct = 74
    applied = []
    logs = []
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
        apply_memory_offset=lambda **_kwargs: {},
        select_expected_vf_samples=lambda _plan: [],
        log=logs.append,
    )
    controller = AdaptiveAutoUvRuntimeController(
        current_tier=PROFILE_TIER_EFFICIENCY,
        vf_curve_reader=reader,
        gpu_policy_controller=object(),
        overlay_state_publisher=overlay,
        dependencies=deps,
    )

    result = controller.update(
        latency_snapshot={"base_present_frametime_p95_ms": 24.0},
        now_monotonic=10.0,
    )

    assert result is not None
    assert result.changed is False
    assert result.tier == PROFILE_TIER_EFFICIENCY
    assert result.reason == "cpu-bound-performance-block"
    assert applied == []
    assert any("held below Performance" in message for message in logs)
