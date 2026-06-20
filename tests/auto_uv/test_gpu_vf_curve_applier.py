from __future__ import annotations

from types import SimpleNamespace

from auto_uv.gpu import gpu_vf_curve_applier


def test_open_live_gpu_applier_applies_auto_uv_power_limit(monkeypatch) -> None:
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
    assert applier.translated_gpu_policy["power_limit_w"] == 390
    assert logs == ["Auto-UV power limit: applied 390W"]
