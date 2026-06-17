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


def _runtime_options():
    return {
        "power_limit_override_w": None,
        "preserve_base_below_mv": None,
        "dangerously_skip_validation": False,
    }


def test_configure_runtime_vf_curve_policy_applies_auto_uv_final_curve_by_default():
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
        prefer_afterburner_curve=False,
        afterburner_root="",
        afterburner_profile="",
        afterburner_device_profile="",
        afterburner_runtime_options=_runtime_options(),
        vf_curve_reader=reader,
        gpu_policy_controller=policy_controller,
        dependencies=deps,
    )

    assert len(base_policy_calls) == 1
    assert base_policy_calls[0]["gpu_policy_controller"] is policy_controller
    assert base_policy_calls[0]["enable_persistence_mode"] is True
    assert base_policy_calls[0]["power_limit_w"] is None
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


def test_configure_runtime_vf_curve_policy_falls_back_to_auto_uv_when_afterburner_fails():
    logs = []
    plan = [{"index": 4, "voltage_mv": 875, "target_mhz": 2760}]
    final_curve = {
        "path": Path("/tmp/auto-uv-final-curve.json"),
        "plan": plan,
        "lock_clock_mhz": 2760,
        "candidate_voltage_mv": 875,
        "memory_offset_mhz": None,
        "flatten_target": {
            "source": "auto-uv-final",
            "lock_clock_mhz": 2760,
            "lock_voltage_mv": 875,
        },
    }
    afterburner_source = {
        "afterburner_root": Path("/ab"),
        "profile_path": Path("/ab/Profile.cfg"),
        "section": "startup",
        "section_info": {},
    }

    def fail_afterburner_apply(*args, **kwargs):
        raise RuntimeError("bad curve")

    deps = RuntimeVfCurvePolicyDependencies(
        load_auto_uv_final_curve=lambda selector: final_curve,
        resolve_afterburner_vf_source=lambda **kwargs: afterburner_source,
        apply_gpu_base_policy=lambda **kwargs: None,
        apply_afterburner_curve_to_reader=fail_afterburner_apply,
        apply_plan=lambda fake_reader, fake_plan: None,
        apply_auto_uv_profile_memory_offset=lambda **kwargs: None,
        flattened_clock_ceiling_controller_factory=FakeClockCeilingController,
        select_expected_vf_samples=lambda applied_plan: ["auto-sample"],
        log=logs.append,
    )

    result = configure_runtime_vf_curve_policy(
        gpu_index=0,
        enable_persistence_mode=False,
        auto_uv_profile_selector="latest",
        prefer_afterburner_curve=True,
        afterburner_root="/ab",
        afterburner_profile="startup",
        afterburner_device_profile="Profile.cfg",
        afterburner_runtime_options=_runtime_options(),
        vf_curve_reader=FakeVfCurveReader(),
        gpu_policy_controller=None,
        dependencies=deps,
    )

    assert result.active_vf_curve_source == "auto-uv-final"
    assert result.vf_expected_samples == ["auto-sample"]
    assert any("Skipping Afterburner VF curve apply" in message for message in logs)
    assert any(
        "trying Auto-UV final curve fallback" in message for message in logs
    )


def test_configure_runtime_vf_curve_policy_applies_afterburner_policy_and_curve():
    logs = []
    base_policy_calls = []
    translated_policy_calls = []
    afterburner_apply_calls = []
    plan = [{"index": 8, "voltage_mv": 900, "target_mhz": 2850}]
    profile_path = Path("/ab/Profile.cfg")
    afterburner_source = {
        "afterburner_root": Path("/ab"),
        "profile_path": profile_path,
        "section": "profile1",
        "section_info": {},
    }
    translated_policy = {"power_limit_w": 220, "mem_clk_vf_offset_mhz": 100}

    class FakeGpuPolicyController:
        def query_power_limits(self):
            return {"min_power_limit_w": 100, "max_power_limit_w": 300}

    def apply_afterburner_curve(fake_reader, **kwargs):
        afterburner_apply_calls.append(kwargs)
        return {
            "plan": plan,
            "changed_points": [plan[0]],
            "translation_mode": "offset",
            "translation_origin": "profile",
            "translated_linux_profile_path": Path("/tmp/linux-profile.json"),
            "materialization": {
                "points": [
                    {"voltage_mv": 900, "target_mhz": 2850},
                    {"voltage_mv": 925, "target_mhz": 2850},
                ]
            },
        }

    deps = RuntimeVfCurvePolicyDependencies(
        load_auto_uv_final_curve=lambda selector: None,
        resolve_afterburner_vf_source=lambda **kwargs: afterburner_source,
        load_afterburner_profile_settings=lambda **kwargs: {"PowerLimit": "90"},
        translate_afterburner_gpu_policy=lambda settings, **kwargs: translated_policy,
        apply_translated_gpu_policy=lambda controller, policy: translated_policy_calls.append(
            (controller, policy)
        ),
        describe_translated_gpu_policy=lambda policy: "power=220W mem=+100MHz",
        apply_gpu_base_policy=lambda **kwargs: base_policy_calls.append(kwargs),
        apply_afterburner_curve_to_reader=apply_afterburner_curve,
        flattened_clock_ceiling_controller_factory=FakeClockCeilingController,
        select_expected_vf_samples=lambda applied_plan: ["afterburner-sample"],
        derive_afterburner_dynamic_lock=lambda points: {
            "source": "afterburner",
            "lock_clock_mhz": 2850,
            "lock_voltage_mv": 900,
        },
        log=logs.append,
    )
    policy_controller = FakeGpuPolicyController()

    result = configure_runtime_vf_curve_policy(
        gpu_index=1,
        enable_persistence_mode=True,
        auto_uv_profile_selector="latest",
        prefer_afterburner_curve=True,
        afterburner_root="/ab",
        afterburner_profile="profile1",
        afterburner_device_profile="Profile.cfg",
        afterburner_runtime_options={
            **_runtime_options(),
            "power_limit_override_w": 230,
            "preserve_base_below_mv": 800,
        },
        vf_curve_reader=FakeVfCurveReader(),
        gpu_policy_controller=policy_controller,
        dependencies=deps,
    )

    assert result.afterburner_profile_settings == {"PowerLimit": "90"}
    assert result.translated_gpu_policy == translated_policy
    assert result.startup_power_limit_w == 220
    assert len(base_policy_calls) == 1
    assert base_policy_calls[0]["gpu_policy_controller"] is policy_controller
    assert base_policy_calls[0]["enable_persistence_mode"] is True
    assert base_policy_calls[0]["power_limit_w"] == 220
    assert translated_policy_calls == [(policy_controller, translated_policy)]
    assert afterburner_apply_calls == [
        {
            "profile_path": profile_path,
            "section": "profile1",
            "gpu_policy": translated_policy,
            "preserve_base_below_mv": 800,
        }
    ]
    assert result.active_vf_curve_source == "afterburner"
    assert result.vf_expected_samples == ["afterburner-sample"]
    assert result.clock_ceiling_controller.applied is True
    assert any("Applied Afterburner GPU policy" in message for message in logs)
    assert any("Applied Afterburner VF curve" in message for message in logs)
