from __future__ import annotations

from types import SimpleNamespace

from auto_uv.gpu import gpu_vf_curve_applier


def test_open_live_gpu_applier_applies_raised_auto_uv_power_limit(monkeypatch) -> None:
    logs: list[str] = []
    controllers: list[FakePolicyController] = []

    class FakeReader:
        def refresh_points(self) -> None:
            return None

    class FakePolicyController:
        def __init__(self, *, gpu_index: int) -> None:
            self.gpu_index = int(gpu_index)
            self.power_limit_calls: list[int] = []
            controllers.append(self)

        def apply_power_limit_w(self, power_limit_w):
            self.power_limit_calls.append(int(power_limit_w))
            return int(power_limit_w)

    monkeypatch.setattr(
        gpu_vf_curve_applier,
        "create_hidden_vf_curve_reader",
        lambda gpu_index: FakeReader(),
    )
    monkeypatch.setattr(
        gpu_vf_curve_applier,
        "reset_nvidia_runtime_defaults",
        lambda **_kwargs: {
            "plan": [{"index": 0, "voltage_mv": 900, "target_mhz": 2500}],
            "gpu_name": "NVIDIA GeForce RTX 5080",
            "power_limit_w": 360,
        },
    )
    monkeypatch.setattr(
        gpu_vf_curve_applier,
        "NvmlGpuPolicyController",
        FakePolicyController,
    )
    monkeypatch.setattr(
        gpu_vf_curve_applier,
        "LiveNvmlVoltageReader",
        lambda gpu_index: SimpleNamespace(close=lambda: None),
    )
    monkeypatch.setattr(gpu_vf_curve_applier, "apply_plan", lambda *_args: None)
    monkeypatch.setattr(
        gpu_vf_curve_applier,
        "assert_zero_runtime_vf_offsets",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        gpu_vf_curve_applier,
        "auto_uv_memory_offset_mhz",
        lambda *_args, **_kwargs: (None, None),
    )

    applier = gpu_vf_curve_applier.open_live_gpu_vf_curve_applier(
        gpu_index=0,
        runtime_options={"auto_uv_power_limit_w": 390},
        log=logs.append,
    )

    assert controllers[0].power_limit_calls == [390]
    assert applier.power_limit_w == 390
    assert applier.baseline_power_limit_w == 360
    assert applier.requested_power_limit_w == 390
    assert applier.translated_gpu_policy["power_limit_w"] == 390
    assert logs == ["Auto-UV power limit: applied 390W"]

    applier.apply_requested_power_limit(log=logs.append)

    assert controllers[0].power_limit_calls == [390]
    assert applier.power_limit_w == 390
    assert applier.translated_gpu_policy["power_limit_w"] == 390
    assert logs == ["Auto-UV power limit: applied 390W"]


def test_open_live_gpu_applier_defers_reduced_auto_uv_power_limit_until_final(
    monkeypatch,
) -> None:
    logs: list[str] = []
    controllers: list[FakePolicyController] = []

    class FakeReader:
        def refresh_points(self) -> None:
            return None

    class FakePolicyController:
        def __init__(self, *, gpu_index: int) -> None:
            self.gpu_index = int(gpu_index)
            self.power_limit_calls: list[int] = []
            controllers.append(self)

        def apply_power_limit_w(self, power_limit_w):
            self.power_limit_calls.append(int(power_limit_w))
            return int(power_limit_w)

    monkeypatch.setattr(
        gpu_vf_curve_applier,
        "create_hidden_vf_curve_reader",
        lambda gpu_index: FakeReader(),
    )
    monkeypatch.setattr(
        gpu_vf_curve_applier,
        "reset_nvidia_runtime_defaults",
        lambda **_kwargs: {
            "plan": [{"index": 0, "voltage_mv": 900, "target_mhz": 2500}],
            "gpu_name": "NVIDIA GeForce RTX 5080",
            "power_limit_w": 360,
        },
    )
    monkeypatch.setattr(
        gpu_vf_curve_applier,
        "NvmlGpuPolicyController",
        FakePolicyController,
    )
    monkeypatch.setattr(
        gpu_vf_curve_applier,
        "LiveNvmlVoltageReader",
        lambda gpu_index: SimpleNamespace(close=lambda: None),
    )
    monkeypatch.setattr(gpu_vf_curve_applier, "apply_plan", lambda *_args: None)
    monkeypatch.setattr(
        gpu_vf_curve_applier,
        "assert_zero_runtime_vf_offsets",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        gpu_vf_curve_applier,
        "auto_uv_memory_offset_mhz",
        lambda *_args, **_kwargs: (None, None),
    )

    applier = gpu_vf_curve_applier.open_live_gpu_vf_curve_applier(
        gpu_index=0,
        runtime_options={"auto_uv_power_limit_w": 319},
        log=logs.append,
    )

    assert controllers[0].power_limit_calls == []
    assert applier.power_limit_w == 360
    assert applier.baseline_power_limit_w == 360
    assert applier.requested_power_limit_w == 319
    assert applier.translated_gpu_policy["power_limit_w"] == 360
    assert logs == [
        "Auto-UV power limit: keeping 360W for discovery/sweep; "
        "will apply 319W for final verification"
    ]

    applier.apply_requested_power_limit(log=logs.append)

    assert controllers[0].power_limit_calls == [319]
    assert applier.power_limit_w == 319
    assert applier.translated_gpu_policy["power_limit_w"] == 319
    assert logs[-1] == "Auto-UV power limit: applied 319W for final verification"


def _patch_applier_environment(monkeypatch, policy_controller_cls) -> None:
    class FakeReader:
        def refresh_points(self) -> None:
            return None

    monkeypatch.setattr(
        gpu_vf_curve_applier,
        "create_hidden_vf_curve_reader",
        lambda gpu_index: FakeReader(),
    )
    monkeypatch.setattr(
        gpu_vf_curve_applier,
        "reset_nvidia_runtime_defaults",
        lambda **_kwargs: {
            "plan": [{"index": 0, "voltage_mv": 900, "target_mhz": 2500}],
            "gpu_name": "NVIDIA GeForce RTX 5080",
            "power_limit_w": 360,
        },
    )
    monkeypatch.setattr(
        gpu_vf_curve_applier,
        "NvmlGpuPolicyController",
        policy_controller_cls,
    )
    monkeypatch.setattr(
        gpu_vf_curve_applier,
        "LiveNvmlVoltageReader",
        lambda gpu_index: SimpleNamespace(close=lambda: None),
    )
    monkeypatch.setattr(gpu_vf_curve_applier, "apply_plan", lambda *_args: None)
    monkeypatch.setattr(
        gpu_vf_curve_applier,
        "assert_zero_runtime_vf_offsets",
        lambda *_args: None,
    )


class _MemoryOffsetPolicyController:
    readback_mhz: int | None = None

    def __init__(self, *, gpu_index: int) -> None:
        self.gpu_index = int(gpu_index)
        self.clock_offset_calls: list[dict] = []

    def get_memory_clock_offset_range_mhz(self):
        return (-2000, 6000)

    def apply_clock_offsets(self, **kwargs):
        self.clock_offset_calls.append(kwargs)
        applied = dict(kwargs)
        applied["mem_clk_vf_offset_readback_mhz"] = self.readback_mhz
        return applied


def test_open_live_gpu_applier_logs_memory_offset_clamp_and_readback(
    monkeypatch,
) -> None:
    logs: list[str] = []

    class ConfirmingController(_MemoryOffsetPolicyController):
        readback_mhz = 6000

    _patch_applier_environment(monkeypatch, ConfirmingController)

    applier = gpu_vf_curve_applier.open_live_gpu_vf_curve_applier(
        gpu_index=0,
        runtime_options={"auto_uv_memory_offset_mhz": 8000},
        log=logs.append,
    )

    # The driver-reported max (6000) is the clamp authority, not the static
    # 2000 fallback cap.
    assert applier.translated_gpu_policy["mem_clk_vf_offset_mhz"] == 6000
    assert applier.policy_controller.clock_offset_calls == [
        {"mem_clk_vf_offset_mhz": 6000}
    ]
    assert (
        "Auto-UV memory offset: requested 8000 MHz clamped to 6000 MHz "
        "(limit 6000 MHz)"
    ) in logs
    assert (
        "Auto-UV memory offset: applied +6000 MHz, "
        "NVML read-back confirms +6000 MHz"
    ) in logs


def test_open_live_gpu_applier_logs_memory_offset_readback_mismatch(
    monkeypatch,
) -> None:
    logs: list[str] = []

    class ClampingController(_MemoryOffsetPolicyController):
        readback_mhz = 500

    _patch_applier_environment(monkeypatch, ClampingController)

    gpu_vf_curve_applier.open_live_gpu_vf_curve_applier(
        gpu_index=0,
        runtime_options={"auto_uv_memory_offset_mhz": 1000},
        log=logs.append,
    )

    assert (
        "Auto-UV memory offset MISMATCH: requested +1000 MHz but NVML reads "
        "back +500 MHz -- the driver clamped or ignored it"
    ) in logs


def test_open_live_gpu_applier_logs_memory_offset_readback_unsupported(
    monkeypatch,
) -> None:
    logs: list[str] = []

    class NoReadbackController(_MemoryOffsetPolicyController):
        readback_mhz = None

    _patch_applier_environment(monkeypatch, NoReadbackController)

    gpu_vf_curve_applier.open_live_gpu_vf_curve_applier(
        gpu_index=0,
        runtime_options={"auto_uv_memory_offset_mhz": 1000},
        log=logs.append,
    )

    assert (
        "Auto-UV memory offset: applied +1000 MHz "
        "(driver does not support read-back)"
    ) in logs
