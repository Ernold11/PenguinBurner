from runtime_fan_control.runtime_loop import (
    RuntimeFanLoopDependencies,
    run_runtime_fan_control_loop,
)
from runtime_gpu_control import RuntimeVfCurvePolicyResult


class FakeNvmlSession:
    def __init__(self, temperatures):
        self._temperatures = list(temperatures)
        self.set_speeds = []
        self.default_calls = []
        self.closed = False

    def fan_count(self):
        return 2

    def fan_speed_limits(self):
        return 20, 90

    def temperature_c(self):
        if len(self._temperatures) > 1:
            return self._temperatures.pop(0)
        return self._temperatures[0]

    def power_draw_w(self):
        return 240.0

    def format_telemetry(
        self,
        *,
        fan_count,
        current_temp_c,
        voltage_reader,
        vf_curve_reader,
        gpu_policy_controller,
        power_draw_w,
        clock_ceiling_controller,
    ):
        return f"temp={current_temp_c:.0f}C power={power_draw_w:.0f}W fans={fan_count}"

    def set_all_fans_speed(self, fan_count, speed_pct):
        self.set_speeds.append((fan_count, speed_pct))

    def set_all_fans_default(self, fan_count):
        self.default_calls.append(fan_count)

    def close(self):
        self.closed = True


class FakeVfCurveReader:
    def __init__(self):
        self.refresh_count = 0

    def summary(self):
        return {"active_points": 12, "editable_core_points": 8}

    def refresh_points(self):
        self.refresh_count += 1


class FakeGpuPolicyController:
    def __init__(self):
        self.clock_offsets = []
        self.closed = False

    def apply_clock_offsets(self, **kwargs):
        self.clock_offsets.append(kwargs)

    def close(self):
        self.closed = True


class FakeOverlayPublisher:
    def __init__(self, *, enabled=True, update_interval_s=2.0, refresh_values=None):
        self.enabled = enabled
        self.update_interval_s = update_interval_s
        self.refresh_values = list(refresh_values or [])
        self.calls = []

    def refresh_config(self):
        if self.refresh_values:
            self.update_interval_s = self.refresh_values.pop(0)

    def publish(self, *, latency_snapshot=None):
        self.calls.append(latency_snapshot)


def _fan_config():
    return {
        "poll_interval_s": 1.0,
        "curve": [[40.0, 0.0], [60.0, 50.0], [80.0, 100.0]],
        "hysteresis_c": 0.0,
        "mode": "linear",
        "min_fan_speed_pct": 0,
        "max_fan_speed_pct": 100,
        "max_step_up_pct_per_s": 0.0,
        "max_step_down_pct_per_s": 0.0,
        "manual_enable_temp_c": 50.0,
        "auto_restore_temp_c": 40.0,
        "emergency_auto_override_temp_c": 80.0,
        "emergency_auto_resume_temp_c": 75.0,
        "force_update_every_poll": False,
    }


def _dependencies(*, logs, prints, sleeps, monotonic_values=None, **overrides):
    monotonic_iter = iter(monotonic_values or [100.0, 101.0, 102.0, 103.0])

    def print_fn(*args, **kwargs):
        prints.append(" ".join(str(arg) for arg in args))

    base = {
        "log": logs.append,
        "print_fn": print_fn,
        "time_monotonic": lambda: next(monotonic_iter),
        "time_sleep": sleeps.append,
        "time_strftime": lambda fmt: "2026-05-13 12:00:00",
        "atexit_register": lambda fn: None,
        "signal_signal": lambda *args: None,
        "exit_process": lambda code: (_ for _ in ()).throw(SystemExit(code)),
        "describe_translated_gpu_policy": lambda policy: f"policy={policy}",
        "detect_vf_curve_reset": lambda reader, samples: [],
    }
    base.update(overrides)
    return RuntimeFanLoopDependencies(**base)


def test_runtime_fan_loop_disabled_logs_telemetry_without_fan_writes():
    logs = []
    prints = []
    sleeps = []
    nvml_session = FakeNvmlSession([55.0])

    run_runtime_fan_control_loop(
        gpu_index=0,
        config_path="/tmp/config.json",
        fan_config={"poll_interval_s": 2.0},
        fan_control_enabled=False,
        enable_persistence_mode=True,
        prefer_afterburner_curve=False,
        nvml_session=nvml_session,
        voltage_reader=None,
        vf_curve_reader=None,
        gpu_policy_controller=None,
        dependencies=_dependencies(logs=logs, prints=prints, sleeps=sleeps),
        max_iterations=1,
    )

    assert "fan control disabled" in prints[0]
    assert nvml_session.set_speeds == []
    assert nvml_session.default_calls == []
    assert sleeps == [2.0]
    # Concise status line; fan control off is shown as "fan off".
    assert any(message.startswith("status |") and "fan off" in message for message in logs)


def test_runtime_fan_loop_status_is_only_logged_on_change():
    # Steady temperature across several polls -> a single status line, not one
    # per iteration (the only-on-change gate keeps the journal quiet).
    logs = []
    prints = []
    sleeps = []
    nvml_session = FakeNvmlSession([55.0])

    run_runtime_fan_control_loop(
        gpu_index=0,
        config_path="/tmp/config.json",
        fan_config={"poll_interval_s": 2.0},
        fan_control_enabled=False,
        enable_persistence_mode=True,
        prefer_afterburner_curve=False,
        nvml_session=nvml_session,
        voltage_reader=None,
        vf_curve_reader=None,
        gpu_policy_controller=None,
        dependencies=_dependencies(
            logs=logs,
            prints=prints,
            sleeps=sleeps,
            monotonic_values=[100.0, 101.0, 102.0, 103.0, 104.0],
        ),
        max_iterations=4,
    )

    status_lines = [m for m in logs if m.startswith("status |")]
    assert len(status_lines) == 1


def test_runtime_fan_loop_keeps_full_detail_in_debug_log():
    # The journal stays concise, but the full per-tick telemetry (which includes
    # the VF curve points) is preserved in the debug log for troubleshooting.
    logs = []
    debug_logs = []
    prints = []
    sleeps = []
    nvml_session = FakeNvmlSession([55.0])

    run_runtime_fan_control_loop(
        gpu_index=0,
        config_path="/tmp/config.json",
        fan_config={"poll_interval_s": 2.0},
        fan_control_enabled=False,
        enable_persistence_mode=True,
        prefer_afterburner_curve=False,
        nvml_session=nvml_session,
        voltage_reader=None,
        vf_curve_reader=None,
        gpu_policy_controller=None,
        dependencies=_dependencies(
            logs=logs, prints=prints, sleeps=sleeps, debug_log=debug_logs.append
        ),
        max_iterations=1,
    )

    # Concise line in the journal, full telemetry in the debug log.
    assert any(message.startswith("status |") for message in logs)
    assert debug_logs == ["temp=55C power=240W fans=2"]


def test_runtime_fan_loop_uses_faster_overlay_update_interval_when_enabled():
    logs = []
    prints = []
    sleeps = []
    nvml_session = FakeNvmlSession([55.0])
    overlay = FakeOverlayPublisher(enabled=True, update_interval_s=1.0)

    run_runtime_fan_control_loop(
        gpu_index=0,
        config_path="/tmp/config.json",
        fan_config={"poll_interval_s": 2.0},
        fan_control_enabled=False,
        enable_persistence_mode=True,
        prefer_afterburner_curve=False,
        nvml_session=nvml_session,
        voltage_reader=None,
        vf_curve_reader=None,
        gpu_policy_controller=None,
        overlay_state_publisher=overlay,
        dependencies=_dependencies(
            logs=logs,
            prints=prints,
            sleeps=sleeps,
            monotonic_values=[100.0, 100.0, 101.0, 102.0],
        ),
        max_iterations=3,
    )

    assert sleeps == [1.0, 1.0, 1.0]
    assert len(overlay.calls) == 3


def test_runtime_fan_loop_throttles_overlay_publish_for_longer_interval():
    logs = []
    prints = []
    sleeps = []
    nvml_session = FakeNvmlSession([55.0])
    overlay = FakeOverlayPublisher(enabled=True, update_interval_s=10.0)

    run_runtime_fan_control_loop(
        gpu_index=0,
        config_path="/tmp/config.json",
        fan_config={"poll_interval_s": 2.0},
        fan_control_enabled=False,
        enable_persistence_mode=True,
        prefer_afterburner_curve=False,
        nvml_session=nvml_session,
        voltage_reader=None,
        vf_curve_reader=None,
        gpu_policy_controller=None,
        overlay_state_publisher=overlay,
        dependencies=_dependencies(
            logs=logs,
            prints=prints,
            sleeps=sleeps,
            monotonic_values=[100.0, 100.0, 102.0, 104.0],
        ),
        max_iterations=3,
    )

    assert sleeps == [2.0, 2.0, 2.0]
    assert len(overlay.calls) == 1


def test_runtime_fan_loop_refreshes_overlay_interval_while_running():
    logs = []
    prints = []
    sleeps = []
    nvml_session = FakeNvmlSession([55.0])
    overlay = FakeOverlayPublisher(
        enabled=True,
        update_interval_s=5.0,
        refresh_values=[1.0, 3.0, 10.0],
    )

    run_runtime_fan_control_loop(
        gpu_index=0,
        config_path="/tmp/config.json",
        fan_config={"poll_interval_s": 5.0},
        fan_control_enabled=False,
        enable_persistence_mode=True,
        prefer_afterburner_curve=False,
        nvml_session=nvml_session,
        voltage_reader=None,
        vf_curve_reader=None,
        gpu_policy_controller=None,
        overlay_state_publisher=overlay,
        dependencies=_dependencies(
            logs=logs,
            prints=prints,
            sleeps=sleeps,
            monotonic_values=[100.0, 100.0, 101.0, 104.0],
        ),
        max_iterations=3,
    )

    assert sleeps == [1.0, 3.0, 5.0]


def test_runtime_fan_loop_enters_manual_mode_and_sets_target_speed():
    logs = []
    prints = []
    sleeps = []
    nvml_session = FakeNvmlSession([60.0])

    run_runtime_fan_control_loop(
        gpu_index=0,
        config_path="/tmp/config.json",
        fan_config=_fan_config(),
        fan_control_enabled=True,
        enable_persistence_mode=True,
        prefer_afterburner_curve=False,
        nvml_session=nvml_session,
        voltage_reader=None,
        vf_curve_reader=None,
        gpu_policy_controller=None,
        dependencies=_dependencies(logs=logs, prints=prints, sleeps=sleeps),
        max_iterations=1,
    )

    assert "Controlling GPU 0 with 2 fan(s)" in prints[0]
    assert "manual-limits=20-90%" in prints[0]
    assert nvml_session.set_speeds == [(2, 50)]
    assert nvml_session.default_calls == []
    # Entering manual is an event line; the steady status shows the fan target.
    assert any(message.startswith("fan ") and "→ manual" in message for message in logs)
    assert any(
        message.startswith("status |") and "fan 50% manual" in message for message in logs
    )


def test_runtime_fan_loop_restores_auto_on_emergency_temperature():
    logs = []
    prints = []
    sleeps = []
    nvml_session = FakeNvmlSession([60.0, 85.0])

    run_runtime_fan_control_loop(
        gpu_index=0,
        config_path="/tmp/config.json",
        fan_config=_fan_config(),
        fan_control_enabled=True,
        enable_persistence_mode=True,
        prefer_afterburner_curve=False,
        nvml_session=nvml_session,
        voltage_reader=None,
        vf_curve_reader=None,
        gpu_policy_controller=None,
        dependencies=_dependencies(
            logs=logs,
            prints=prints,
            sleeps=sleeps,
            monotonic_values=[100.0, 101.0, 102.0],
        ),
        max_iterations=2,
    )

    assert nvml_session.set_speeds == [(2, 50)]
    assert nvml_session.default_calls == [2]
    # Emergency override is its own tagged event line.
    assert any(message.startswith("emerg ") and "override" in message for message in logs)


def test_runtime_fan_loop_reapplies_vf_curve_after_reset_detection():
    logs = []
    prints = []
    sleeps = []
    applied_plans = []
    nvml_session = FakeNvmlSession([55.0])
    vf_curve_reader = FakeVfCurveReader()
    plan = [{"index": 4, "new_offset_mhz": 500}]

    run_runtime_fan_control_loop(
        gpu_index=0,
        config_path="/tmp/config.json",
        fan_config={"poll_interval_s": 1.0},
        fan_control_enabled=False,
        enable_persistence_mode=True,
        prefer_afterburner_curve=False,
        nvml_session=nvml_session,
        voltage_reader=None,
        vf_curve_reader=vf_curve_reader,
        gpu_policy_controller=None,
        vf_policy=RuntimeVfCurvePolicyResult(
            vf_apply_result={"plan": plan},
            vf_expected_samples=["sample"],
        ),
        dependencies=_dependencies(
            logs=logs,
            prints=prints,
            sleeps=sleeps,
            monotonic_values=[100.0, 111.0],
            detect_vf_curve_reset=lambda reader, samples: [{"index": 4}],
            apply_plan=lambda reader, applied_plan: applied_plans.append(
                (reader, applied_plan)
            ),
            format_vf_curve_mismatch_preview=lambda mismatches: "idx=4",
        ),
        max_iterations=1,
    )

    assert applied_plans == [(vf_curve_reader, plan)]
    assert vf_curve_reader.refresh_count == 1
    assert any("event=vf-curve-reapplied" in message for message in logs)


def test_runtime_fan_loop_reapplies_memory_offset_after_vf_curve_reset():
    logs = []
    prints = []
    sleeps = []
    applied_plans = []
    nvml_session = FakeNvmlSession([55.0])
    vf_curve_reader = FakeVfCurveReader()
    gpu_policy_controller = FakeGpuPolicyController()
    plan = [{"index": 4, "new_offset_mhz": 500}]

    run_runtime_fan_control_loop(
        gpu_index=0,
        config_path="/tmp/config.json",
        fan_config={"poll_interval_s": 1.0},
        fan_control_enabled=False,
        enable_persistence_mode=True,
        prefer_afterburner_curve=False,
        nvml_session=nvml_session,
        voltage_reader=None,
        vf_curve_reader=vf_curve_reader,
        gpu_policy_controller=gpu_policy_controller,
        vf_policy=RuntimeVfCurvePolicyResult(
            vf_apply_result={"plan": plan},
            vf_expected_samples=["sample"],
            auto_uv_profile_gpu_policy={"mem_clk_vf_offset_mhz": 750},
        ),
        dependencies=_dependencies(
            logs=logs,
            prints=prints,
            sleeps=sleeps,
            monotonic_values=[100.0, 111.0],
            detect_vf_curve_reset=lambda reader, samples: [{"index": 4}],
            apply_plan=lambda reader, applied_plan: applied_plans.append(
                (reader, applied_plan)
            ),
            format_vf_curve_mismatch_preview=lambda mismatches: "idx=4",
        ),
        max_iterations=1,
    )

    assert applied_plans == [(vf_curve_reader, plan)]
    assert gpu_policy_controller.clock_offsets == [
        {"mem_clk_vf_offset_mhz": 750}
    ]
    assert any("event=vf-curve-reapplied" in message for message in logs)
