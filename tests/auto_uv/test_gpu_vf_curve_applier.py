from __future__ import annotations

from typing import Any, cast

from auto_uv.gpu import gpu_vf_curve_applier
from runtime.support import nvidia_runtime_defaults as runtime_defaults


_POWER_LIMIT_ERROR = (
    "nvmlDeviceSetPowerManagementLimit failed with NVML error 3: Not Supported"
)


def _patch_applier_environment(monkeypatch, gpu_client_type) -> None:
    monkeypatch.setattr(gpu_vf_curve_applier, "DaemonGpuClient", gpu_client_type)
    monkeypatch.setattr(
        gpu_vf_curve_applier,
        "reset_nvidia_runtime_defaults",
        lambda **_kwargs: {
            "plan": [{"index": 0, "voltage_mv": 900, "target_mhz": 2500}],
            "gpu_name": "NVIDIA GeForce RTX 5080",
            "power_limit_w": 360,
        },
    )
    monkeypatch.setattr(gpu_vf_curve_applier, "apply_plan", lambda *_args: None)
    monkeypatch.setattr(
        gpu_vf_curve_applier, "assert_zero_runtime_vf_offsets", lambda *_args: None
    )


def _patch_power_environment(monkeypatch, *, error: str | None = None):
    clients = []

    class FakeGpuClient:
        def __init__(self, *, gpu_index: int) -> None:
            self.gpu_index = int(gpu_index)
            self.power_limit_calls: list[int] = []
            clients.append(self)

        def refresh_points(self) -> None:
            pass

        def apply_power_limit_w(self, power_limit_w):
            self.power_limit_calls.append(int(power_limit_w))
            if error is not None:
                raise RuntimeError(error)
            return int(power_limit_w)

    _patch_applier_environment(monkeypatch, FakeGpuClient)
    monkeypatch.setattr(
        gpu_vf_curve_applier,
        "auto_uv_memory_offset_mhz",
        lambda *_args, **_kwargs: (None, None),
    )
    return clients


def test_open_live_gpu_applier_applies_raised_auto_uv_power_limit(monkeypatch) -> None:
    logs: list[str] = []
    controllers = _patch_power_environment(monkeypatch)

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
    controllers = _patch_power_environment(monkeypatch)

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


def test_open_live_gpu_applier_continues_when_raised_power_limit_is_rejected(
    monkeypatch,
) -> None:
    logs: list[str] = []
    controllers = _patch_power_environment(monkeypatch, error=_POWER_LIMIT_ERROR)

    applier = gpu_vf_curve_applier.open_live_gpu_vf_curve_applier(
        gpu_index=0,
        runtime_options={"auto_uv_power_limit_w": 390},
        log=logs.append,
    )

    assert controllers[0].power_limit_calls == [390]
    assert applier.power_limit_w is None
    assert applier.requested_power_limit_w is None
    assert "power_limit_w" not in applier.translated_gpu_policy
    assert logs == [
        "Auto-UV power limit: unable to apply 390W; "
        "continuing without saved power limit: "
        "nvmlDeviceSetPowerManagementLimit failed with NVML error 3: "
        "Not Supported"
    ]


def test_final_power_limit_rejection_is_not_fatal_and_not_saved() -> None:
    logs: list[str] = []

    class FakePolicyController:
        def __init__(self) -> None:
            self.power_limit_calls: list[int] = []

        def apply_power_limit_w(self, power_limit_w):
            self.power_limit_calls.append(int(power_limit_w))
            raise RuntimeError(
                "nvmlDeviceSetPowerManagementLimit failed with NVML error 3: "
                "Not Supported"
            )

    policy_controller = FakePolicyController()
    applier = gpu_vf_curve_applier.LiveGpuVfCurveApplier(
        gpu_index=0,
        gpu=cast(Any, policy_controller),
        runtime_default_plan=[],
        translated_gpu_policy={"power_limit_w": 360},
        baseline_power_limit_w=360,
        requested_power_limit_w=43,
    )

    assert applier.apply_requested_power_limit(log=logs.append) is None

    assert policy_controller.power_limit_calls == [43]
    assert applier.power_limit_w is None
    assert applier.requested_power_limit_w is None
    assert "power_limit_w" not in applier.translated_gpu_policy
    assert logs == [
        "Auto-UV power limit: unable to apply 43W for final verification; "
        "continuing without saved power limit: "
        "nvmlDeviceSetPowerManagementLimit failed with NVML error 3: "
        "Not Supported"
    ]


class _MemoryOffsetPolicyController:
    readback_mhz: int | None = None

    def __init__(self, *, gpu_index: int) -> None:
        self.gpu_index = int(gpu_index)
        self.clock_offset_calls: list[dict] = []

    def get_memory_clock_offset_range_mhz(self):
        return (-2000, 6000)

    def refresh_points(self) -> None:
        return None

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
    assert cast(Any, applier.policy_controller).clock_offset_calls == [
        {"mem_clk_vf_offset_mhz": 6000}
    ]
    assert (
        "Auto-UV memory offset: requested 8000 MHz clamped to 6000 MHz (limit 6000 MHz)"
    ) in logs
    assert (
        "Auto-UV memory offset: applied +6000 MHz, NVML read-back confirms +6000 MHz"
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
        "Auto-UV memory offset: applied +1000 MHz (driver does not support read-back)"
    ) in logs


def test_runtime_defaults_are_derived_from_semantic_daemon_reset(monkeypatch) -> None:
    logs: list[str] = []
    monkeypatch.setattr(
        runtime_defaults,
        "gpu_reset_defaults",
        lambda gpu_index: {
            "reset": True,
            "gpu_name": "NVIDIA GeForce RTX 5080",
            "power_limits": {"power_limit_default_w": 360},
            "points": [
                {
                    "index": 12,
                    "type": 0,
                    "voltage_based": 1,
                    "voltage_uv": 900_000,
                    "base_freq_khz": 2_680_000,
                    "current_offset_khz": 0,
                }
            ],
        },
    )

    result = runtime_defaults.reset_nvidia_runtime_defaults(
        gpu_index=0,
        log=logs.append,
    )

    assert result["power_limit_w"] == 360
    assert result["plan"] == [
        {
            "index": 12,
            "voltage_mv": 900,
            "base_mhz": 2680,
            "target_mhz": 2680,
            "current_offset_mhz": 0,
            "new_offset_mhz": 0,
            "preserve_base": False,
        }
    ]
    assert logs == [
        "Reset defaults through penguin-burnerd: "
        "gpu=NVIDIA GeForce RTX 5080 points=1 power_limit=360W"
    ]


def test_runtime_defaults_reject_daemon_reset_without_editable_points(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        runtime_defaults,
        "gpu_reset_defaults",
        lambda gpu_index: {"reset": True, "points": []},
    )

    try:
        runtime_defaults.reset_nvidia_runtime_defaults(gpu_index=0)
    except runtime_defaults.NvidiaRuntimeDefaultsError as exc:
        assert "no editable V/F points" in str(exc)
    else:
        raise AssertionError("missing editable V/F points must fail Auto-UV startup")
