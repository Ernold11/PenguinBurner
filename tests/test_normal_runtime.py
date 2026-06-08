from types import SimpleNamespace

from cli.normal_runtime import NormalRuntimeDependencies, run_normal_runtime


def _command_route(**overrides):
    values = {
        "config_path": "/tmp/config.json",
        "gpu_config": {"index": 0, "enable_persistence_mode": True},
        "fan_config": {"poll_interval_s": 1.0},
        "gpu_index": 0,
        "afterburner_runtime_options": {
            "afterburner_root": "",
            "afterburner_profile": "",
            "afterburner_device_profile": "",
        },
        "prefer_afterburner_curve": False,
        "auto_uv_profile_selector": "",
        "auto_uv_final_curve_available": False,
        "had_persisted_afterburner_root": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_normal_runtime_returns_when_startup_preparation_handles_flow():
    calls = []

    def prepare(**kwargs):
        calls.append(("prepare", kwargs))
        return SimpleNamespace(should_exit=True)

    deps = NormalRuntimeDependencies(
        prepare_runtime_startup=prepare,
        nvml_session_factory=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("NVML should not be opened")
        ),
    )

    run_normal_runtime(
        args=SimpleNamespace(silent_fan_curve=True),
        argv=["--silent-fan-curve"],
        journal_hours=24,
        program_file="/tmp/penguin_burner.py",
        command_route=_command_route(),
        prompt_yes_no=lambda *args, **kwargs: False,
        interactive=True,
        startup_dependencies="startup-deps",
        vf_curve_policy_dependencies="vf-deps",
        fan_loop_dependencies="fan-deps",
        dependencies=deps,
    )

    assert calls[0][0] == "prepare"
    assert calls[0][1]["fan_control_enabled"] is True
    assert calls[0][1]["dependencies"] == "startup-deps"


def test_normal_runtime_wires_startup_vf_policy_and_fan_loop():
    calls = []
    nvml_session = object()
    voltage_reader = object()
    vf_curve_reader = object()
    gpu_policy_controller = object()
    vf_policy = SimpleNamespace(
        translated_gpu_policy={"power_limit_w": 240},
        afterburner_source={"section": "startup"},
        afterburner_profile_settings={"PowerLimit": "90"},
        auto_uv_final_curve={"path": "/tmp/final.json"},
        vf_apply_result={"plan": []},
        active_vf_curve_source="auto-uv-final",
        auto_uv_profile_gpu_policy={"mem_clk_vf_offset_mhz": 500},
        clock_ceiling_controller=object(),
        vf_expected_samples=["sample"],
        startup_power_limit_w=240,
    )

    def prepare(**kwargs):
        calls.append(("prepare", kwargs))
        return SimpleNamespace(
            should_exit=False,
            afterburner_runtime_options={"afterburner_root": "/ab"},
            fan_config={"poll_interval_s": 0.5},
            fan_control_enabled=True,
            afterburner_root="/ab",
            afterburner_profile="profile1",
            afterburner_device_profile="VEN.cfg",
        )

    def configure(**kwargs):
        calls.append(("configure", kwargs))
        return vf_policy

    def run_loop(**kwargs):
        calls.append(("loop", kwargs))

    deps = NormalRuntimeDependencies(
        prepare_runtime_startup=prepare,
        nvml_session_factory=lambda **kwargs: nvml_session,
        create_hidden_voltage_reader=lambda **kwargs: voltage_reader,
        create_hidden_vf_curve_reader=lambda **kwargs: vf_curve_reader,
        gpu_policy_controller_factory=lambda **kwargs: gpu_policy_controller,
        configure_runtime_vf_curve_policy=configure,
        run_runtime_fan_control_loop=run_loop,
        start_latency_telemetry_logger=lambda **kwargs: None,
    )

    run_normal_runtime(
        args=SimpleNamespace(silent_fan_curve=True),
        argv=["--silent-fan-curve"],
        journal_hours=24,
        program_file="/tmp/penguin_burner.py",
        command_route=_command_route(
            gpu_index=2,
            gpu_config={"index": 2, "enable_persistence_mode": False},
            prefer_afterburner_curve=True,
            auto_uv_profile_selector="latest",
            auto_uv_final_curve_available=True,
            had_persisted_afterburner_root=True,
        ),
        prompt_yes_no=lambda *args, **kwargs: False,
        interactive=False,
        startup_dependencies="startup-deps",
        vf_curve_policy_dependencies="vf-deps",
        fan_loop_dependencies="fan-deps",
        dependencies=deps,
    )

    configure_call = calls[1][1]
    assert configure_call["gpu_index"] == 2
    assert configure_call["enable_persistence_mode"] is False
    assert configure_call["auto_uv_profile_selector"] == "latest"
    assert configure_call["prefer_afterburner_curve"] is True
    assert configure_call["afterburner_root"] == "/ab"
    assert configure_call["afterburner_profile"] == "profile1"
    assert configure_call["afterburner_device_profile"] == "VEN.cfg"
    assert configure_call["vf_curve_reader"] is vf_curve_reader
    assert configure_call["gpu_policy_controller"] is gpu_policy_controller
    assert configure_call["dependencies"] == "vf-deps"

    loop_call = calls[2][1]
    assert loop_call["gpu_index"] == 2
    assert loop_call["fan_config"] == {"poll_interval_s": 0.5}
    assert loop_call["fan_control_enabled"] is True
    assert loop_call["enable_persistence_mode"] is False
    assert loop_call["nvml_session"] is nvml_session
    assert loop_call["voltage_reader"] is voltage_reader
    assert loop_call["vf_curve_reader"] is vf_curve_reader
    assert loop_call["gpu_policy_controller"] is gpu_policy_controller
    assert loop_call["vf_policy"] is vf_policy
    assert loop_call["dependencies"] == "fan-deps"


def test_normal_runtime_logs_gpu_policy_helper_failure_and_continues():
    logs = []
    vf_policy = SimpleNamespace(
        translated_gpu_policy=None,
        afterburner_source=None,
        afterburner_profile_settings=None,
        auto_uv_final_curve=None,
        vf_apply_result=None,
        active_vf_curve_source=None,
        auto_uv_profile_gpu_policy=None,
        clock_ceiling_controller=None,
        vf_expected_samples=[],
        startup_power_limit_w=None,
    )
    loop_calls = []
    configure_calls = []

    def prepare(**kwargs):
        return SimpleNamespace(
            should_exit=False,
            afterburner_runtime_options={},
            fan_config={},
            fan_control_enabled=False,
            afterburner_root="",
            afterburner_profile="",
            afterburner_device_profile="",
        )

    def configure(**kwargs):
        configure_calls.append(kwargs)
        return vf_policy

    deps = NormalRuntimeDependencies(
        prepare_runtime_startup=prepare,
        nvml_session_factory=lambda **kwargs: object(),
        create_hidden_voltage_reader=lambda **kwargs: object(),
        create_hidden_vf_curve_reader=lambda **kwargs: object(),
        gpu_policy_controller_factory=lambda **kwargs: (_ for _ in ()).throw(
            RuntimeError("policy unsupported")
        ),
        configure_runtime_vf_curve_policy=configure,
        run_runtime_fan_control_loop=lambda **kwargs: loop_calls.append(kwargs),
        start_latency_telemetry_logger=lambda **kwargs: None,
        log=logs.append,
    )

    run_normal_runtime(
        args=SimpleNamespace(silent_fan_curve=False),
        argv=[],
        journal_hours=24,
        program_file="/tmp/penguin_burner.py",
        command_route=_command_route(),
        prompt_yes_no=lambda *args, **kwargs: False,
        interactive=False,
        dependencies=deps,
    )

    assert configure_calls[0]["gpu_policy_controller"] is None
    assert loop_calls[0]["gpu_policy_controller"] is None
    assert logs == ["Linux GPU policy helper unavailable: policy unsupported"]
