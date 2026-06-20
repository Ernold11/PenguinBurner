from pathlib import Path

from runtime_gpu_control.vf_curve_runtime_policy import (
    RuntimeVfCurvePolicyDependencies,
    configure_runtime_vf_curve_policy,
)


class FakeVfCurveReader:
    def __init__(self):
        self.refresh_count = 0

    def refresh_points(self):
        self.refresh_count += 1


class FakeClockCeilingController:
    def __init__(self, *, flatten_target, policy_controller):
        self.flatten_target = dict(flatten_target)
        self.policy_controller = policy_controller
        self.applied = False

    def apply(self):
        self.applied = True

    def describe(self):
        return f"{self.flatten_target['lock_clock_mhz']}MHz ceiling"


def test_configure_runtime_vf_curve_policy_applies_auto_uv_final_curve():
    logs = []
    base_policy_calls = []
    applied_plans = []
    reader = FakeVfCurveReader()
    plan = [
        {
            "index": 4,
            "voltage_mv": 875,
            "target_mhz": 2760,
            "new_offset_mhz": 430,
        }
    ]
    final_curve = {
        "path": Path("/tmp/auto-uv-final-curve.json"),
        "plan": plan,
        "lock_clock_mhz": 2760,
        "candidate_voltage_mv": 875,
        "memory_offset_mhz": 500,
        "flatten_target": {
            "source": "auto-uv-final",
            "lock_clock_mhz": 2760,
            "lock_voltage_mv": 875,
        },
    }

    deps = RuntimeVfCurvePolicyDependencies(
        load_auto_uv_final_curve=lambda selector: final_curve,
        apply_gpu_base_policy=lambda **kwargs: base_policy_calls.append(kwargs),
        apply_plan=lambda fake_reader, fake_plan: applied_plans.append(
            (fake_reader, fake_plan)
        ),
        apply_auto_uv_profile_memory_offset=lambda **kwargs: {
            "mem_clk_vf_offset_mhz": int(kwargs["memory_offset_mhz"])
        },
        flattened_clock_ceiling_controller_factory=FakeClockCeilingController,
        select_expected_vf_samples=lambda applied_plan: ["expected-sample"],
        log=logs.append,
    )
    policy_controller = object()

    result = configure_runtime_vf_curve_policy(
        gpu_index=0,
        enable_persistence_mode=True,
        auto_uv_profile_selector="verified",
        vf_curve_reader=reader,
        gpu_policy_controller=policy_controller,
        dependencies=deps,
    )

    assert len(base_policy_calls) == 1
    assert base_policy_calls[0]["gpu_policy_controller"] is policy_controller
    assert base_policy_calls[0]["enable_persistence_mode"] is True
    assert applied_plans == [(reader, plan)]
    assert reader.refresh_count == 1
    assert result.active_vf_curve_source == "auto-uv-final"
    assert result.vf_apply_result == {
        "source": "auto-uv-final",
        "plan": plan,
        "path": final_curve["path"],
    }
    assert result.vf_expected_samples == ["expected-sample"]
    assert result.auto_uv_profile_gpu_policy == {"mem_clk_vf_offset_mhz": 500}
    assert result.clock_ceiling_controller.applied is True
    assert any("Applied auto-UV final curve" in message for message in logs)


def test_configure_runtime_vf_curve_policy_applies_auto_uv_profile_power_limit():
    logs = []
    power_limit_calls = []
    reader = FakeVfCurveReader()
    plan = [{"index": 4, "voltage_mv": 875, "target_mhz": 2760}]
    final_curve = {
        "path": Path("/tmp/auto-uv-final-curve.json"),
        "plan": plan,
        "lock_clock_mhz": 2760,
        "candidate_voltage_mv": 875,
        "memory_offset_mhz": None,
        "power_limit_w": 360,
        "flatten_target": {
            "source": "auto-uv-final",
            "lock_clock_mhz": 2760,
            "lock_voltage_mv": 875,
        },
    }

    deps = RuntimeVfCurvePolicyDependencies(
        load_auto_uv_final_curve=lambda selector: final_curve,
        apply_gpu_base_policy=lambda **kwargs: None,
        apply_plan=lambda fake_reader, fake_plan: None,
        apply_auto_uv_profile_power_limit=lambda **kwargs: (
            power_limit_calls.append(kwargs) or {"power_limit_w": 360}
        ),
        apply_auto_uv_profile_memory_offset=lambda **kwargs: {},
        flattened_clock_ceiling_controller_factory=FakeClockCeilingController,
        select_expected_vf_samples=lambda applied_plan: [],
        log=logs.append,
    )
    policy_controller = object()

    result = configure_runtime_vf_curve_policy(
        gpu_index=0,
        enable_persistence_mode=True,
        auto_uv_profile_selector="verified",
        vf_curve_reader=reader,
        gpu_policy_controller=policy_controller,
        dependencies=deps,
    )

    assert power_limit_calls == [
        {
            "profile_label": "auto-UV final curve",
            "power_limit_w": 360,
            "gpu_policy_controller": policy_controller,
        }
    ]
    assert result.auto_uv_profile_gpu_policy == {"power_limit_w": 360}
    assert any("Applied auto-UV profile power limit: 360W" in m for m in logs)


def test_configure_runtime_vf_curve_policy_skips_curve_when_no_auto_uv_profile():
    logs = []
    base_policy_calls = []

    deps = RuntimeVfCurvePolicyDependencies(
        load_auto_uv_final_curve=lambda selector: None,
        apply_gpu_base_policy=lambda **kwargs: base_policy_calls.append(kwargs),
        log=logs.append,
    )

    result = configure_runtime_vf_curve_policy(
        gpu_index=0,
        enable_persistence_mode=False,
        auto_uv_profile_selector="",
        vf_curve_reader=FakeVfCurveReader(),
        gpu_policy_controller=None,
        dependencies=deps,
    )

    assert len(base_policy_calls) == 1
    assert result.active_vf_curve_source is None
    assert result.vf_apply_result is None
