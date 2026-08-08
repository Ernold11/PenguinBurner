from __future__ import annotations

from typing import Any, cast

import pytest

from auto_uv.domain.types import AutoUvPowerLimitApplyError
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


def _patch_power_environment(
    monkeypatch,
    *,
    error: str | None = None,
    power_limit_supported: bool = True,
    power_limit_probe_error: Exception | None = None,
):
    clients = []

    class FakeGpuClient:
        def __init__(self, *, gpu_index: int) -> None:
            self.gpu_index = int(gpu_index)
            self.power_limit_calls: list[int] = []
            self.power_limit_probe_calls = 0
            clients.append(self)

        def refresh_points(self) -> None:
            pass

        def apply_power_limit_w(self, power_limit_w):
            self.power_limit_calls.append(int(power_limit_w))
            if error is not None:
                raise RuntimeError(error)
            return int(power_limit_w)

        def power_limit_set_supported(self) -> bool:
            self.power_limit_probe_calls += 1
            if power_limit_probe_error is not None:
                raise power_limit_probe_error
            return bool(power_limit_supported)

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
    assert logs == [
        "Auto-UV power limit: applied 390W for discovery and sweep"
    ]

    applier.apply_requested_power_limit(log=logs.append)

    assert controllers[0].power_limit_calls == [390]
    assert applier.power_limit_w == 390
    assert applier.translated_gpu_policy["power_limit_w"] == 390
    assert logs == [
        "Auto-UV power limit: applied 390W for discovery and sweep"
    ]


def test_open_live_gpu_applier_applies_reduced_auto_uv_power_limit_for_scan(
    monkeypatch,
) -> None:
    logs: list[str] = []
    controllers = _patch_power_environment(monkeypatch)

    applier = gpu_vf_curve_applier.open_live_gpu_vf_curve_applier(
        gpu_index=0,
        runtime_options={"auto_uv_power_limit_w": 319},
        log=logs.append,
    )

    assert controllers[0].power_limit_calls == [319]
    assert applier.power_limit_w == 319
    assert applier.baseline_power_limit_w == 360
    assert applier.requested_power_limit_w == 319
    assert applier.translated_gpu_policy["power_limit_w"] == 319
    assert logs == [
        "Auto-UV power limit: applied 319W for discovery and sweep"
    ]

    applier.apply_requested_power_limit(log=logs.append)

    assert controllers[0].power_limit_calls == [319]
    assert applier.power_limit_w == 319
    assert applier.translated_gpu_policy["power_limit_w"] == 319
    assert logs == [
        "Auto-UV power limit: applied 319W for discovery and sweep"
    ]


def test_open_live_gpu_applier_stops_when_raised_power_limit_is_rejected(
    monkeypatch,
) -> None:
    logs: list[str] = []
    controllers = _patch_power_environment(monkeypatch, error=_POWER_LIMIT_ERROR)

    with pytest.raises(
        AutoUvPowerLimitApplyError,
        match="scan stopped before probing",
    ):
        gpu_vf_curve_applier.open_live_gpu_vf_curve_applier(
            gpu_index=0,
            runtime_options={"auto_uv_power_limit_w": 390},
            log=logs.append,
        )

    assert controllers[0].power_limit_calls == [390]
    assert logs == []


def test_unsupported_power_limit_is_platform_managed_and_not_saved(
    monkeypatch,
) -> None:
    logs: list[str] = []
    controllers = _patch_power_environment(
        monkeypatch,
        power_limit_supported=False,
    )
    monkeypatch.setattr(
        gpu_vf_curve_applier,
        "reset_nvidia_runtime_defaults",
        lambda **_kwargs: {
            "plan": [{"index": 0, "voltage_mv": 900, "target_mhz": 2490}],
            "gpu_name": "NVIDIA GeForce RTX 4090 Laptop GPU",
            "power_limit_w": 150,
            "power_limits": {
                "power_limit_default_w": 150,
                "power_limit_min_w": 5,
                "power_limit_max_w": 175,
            },
        },
    )
    applier = gpu_vf_curve_applier.open_live_gpu_vf_curve_applier(
        gpu_index=0,
        runtime_options={},
        log=logs.append,
    )

    assert controllers[0].power_limit_calls == []
    assert applier.power_limit_w is None
    assert applier.baseline_power_limit_w is None
    assert applier.requested_power_limit_w is None
    assert "power_limit_w" not in applier.translated_gpu_policy
    assert logs == [
        "Auto-UV power limit: driver reports fixed power-limit writes are unsupported; "
        "continuing without saved power limit"
    ]

    applier.requested_power_limit_w = 150
    assert applier.apply_requested_power_limit(log=logs.append) is None
    assert controllers[0].power_limit_calls == []
    assert logs[-1] == (
        "Auto-UV power limit: skipped unsupported fixed write for final verification; "
        "continuing without saved power limit"
    )


def test_successful_probe_is_authoritative_even_for_mobile_identity_strings(
    monkeypatch,
) -> None:
    logs: list[str] = []
    controllers = _patch_power_environment(monkeypatch)
    monkeypatch.setattr(
        gpu_vf_curve_applier,
        "reset_nvidia_runtime_defaults",
        lambda **_kwargs: {
            "plan": [{"index": 0, "voltage_mv": 900, "target_mhz": 2490}],
            "gpu_name": "NVIDIA GeForce RTX 5090",
            "pci_device_id": "0x2C1810DE",
            "power_limit_w": 150,
            "power_limits": {
                "power_limit_default_w": 150,
                "power_limit_min_w": 5,
                "power_limit_max_w": 175,
            },
        },
    )

    applier = gpu_vf_curve_applier.open_live_gpu_vf_curve_applier(
        gpu_index=0,
        runtime_options={"auto_uv_power_limit_w": 175},
        log=logs.append,
    )

    assert controllers[0].power_limit_calls == [175]
    assert applier.power_limit_w == 175
    assert applier.baseline_power_limit_w == 150
    assert applier.requested_power_limit_w == 175
    assert applier.translated_gpu_policy["pci_device_id"] == "0x2C1810DE"
    assert applier.translated_gpu_policy["power_limit_w"] == 175
    assert logs == ["Auto-UV power limit: applied 175W for discovery and sweep"]


def test_unrecognized_rtx_2050_with_unsupported_setter_never_saves_power_limit(
    monkeypatch,
) -> None:
    logs: list[str] = []
    controllers = _patch_power_environment(
        monkeypatch,
        power_limit_supported=False,
    )
    monkeypatch.setattr(
        gpu_vf_curve_applier,
        "reset_nvidia_runtime_defaults",
        lambda **_kwargs: {
            "plan": [{"index": 0, "voltage_mv": 900, "target_mhz": 1620}],
            "gpu_name": "NVIDIA GeForce RTX 2050",
            "pci_device_id": "0x25B810DE",
            "power_limit_w": 35,
            "power_limits": {
                "power_limit_default_w": 35,
                "power_limit_min_w": 35,
                "power_limit_max_w": 55,
            },
        },
    )
    applier = gpu_vf_curve_applier.open_live_gpu_vf_curve_applier(
        gpu_index=0,
        runtime_options={},
        log=logs.append,
    )

    assert controllers[0].power_limit_probe_calls == 1
    assert controllers[0].power_limit_calls == []
    assert applier.power_limit_w is None
    assert applier.baseline_power_limit_w is None
    assert "power_limit_w" not in applier.translated_gpu_policy
    assert logs == [
        "Auto-UV power limit: driver reports fixed power-limit writes are unsupported; "
        "continuing without saved power limit"
    ]


def test_power_limit_probe_failure_disables_power_without_claiming_driver_result(
    monkeypatch,
) -> None:
    logs: list[str] = []
    _patch_power_environment(
        monkeypatch,
        power_limit_probe_error=RuntimeError("daemon timeout"),
    )

    applier = gpu_vf_curve_applier.open_live_gpu_vf_curve_applier(
        gpu_index=0,
        runtime_options={},
        log=logs.append,
    )

    assert applier.power_limit_set_supported is False
    assert "power_limit_w" not in applier.translated_gpu_policy
    assert logs == [
        "Auto-UV power limit: could not verify fixed power-limit write support "
        "(daemon timeout); continuing without saved power limit"
    ]


def test_tier_power_limit_rejection_is_fatal_and_preserves_known_cap() -> None:
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

    with pytest.raises(
        AutoUvPowerLimitApplyError,
        match="probing under an unknown power regime",
    ):
        applier.apply_requested_power_limit(log=logs.append)

    assert policy_controller.power_limit_calls == [43]
    assert applier.power_limit_w == 360
    assert applier.requested_power_limit_w is None
    assert applier.translated_gpu_policy["power_limit_w"] == 360
    assert logs == []


def test_power_limit_readback_mismatch_stops_before_policy_is_updated() -> None:
    class FakePolicyController:
        def apply_power_limit_w(self, power_limit_w):
            return int(power_limit_w)

        def capabilities(self, *, refresh):
            assert refresh
            return cast(
                Any,
                type(
                    "Caps",
                    (),
                    {
                        "power": type(
                            "Power",
                            (),
                            {"current_w": 300.0, "enforced_w": 300.0},
                        )()
                    },
                )(),
            )

    applier = gpu_vf_curve_applier.LiveGpuVfCurveApplier(
        gpu_index=0,
        gpu=cast(Any, FakePolicyController()),
        runtime_default_plan=[],
        translated_gpu_policy={"power_limit_w": 360},
        baseline_power_limit_w=360,
        requested_power_limit_w=320,
    )

    with pytest.raises(AutoUvPowerLimitApplyError, match="read-back mismatch"):
        applier.apply_requested_power_limit(log=lambda _message: None)

    assert applier.power_limit_w == 360
    assert applier.requested_power_limit_w is None


def test_power_limit_readback_error_stops_before_policy_is_updated() -> None:
    class FakePolicyController:
        def apply_power_limit_w(self, power_limit_w):
            return int(power_limit_w)

        def capabilities(self, *, refresh):
            assert refresh
            raise RuntimeError("daemon read-back unavailable")

    applier = gpu_vf_curve_applier.LiveGpuVfCurveApplier(
        gpu_index=0,
        gpu=cast(Any, FakePolicyController()),
        runtime_default_plan=[],
        translated_gpu_policy={"power_limit_w": 360},
        baseline_power_limit_w=360,
        requested_power_limit_w=320,
    )

    with pytest.raises(AutoUvPowerLimitApplyError, match="unable to read back"):
        applier.apply_requested_power_limit(log=lambda _message: None)

    assert applier.power_limit_w == 360
    assert applier.requested_power_limit_w is None


class _MemoryOffsetPolicyController:
    readback_mhz: int | None = None

    def __init__(self, *, gpu_index: int) -> None:
        self.gpu_index = int(gpu_index)
        self.clock_offset_calls: list[dict] = []

    def get_memory_clock_offset_range_mhz(self):
        return (-2000, 6000)

    def refresh_points(self) -> None:
        return None

    def power_limit_set_supported(self) -> bool:
        return True

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
