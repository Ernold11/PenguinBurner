from __future__ import annotations

from runtime.support import nvidia_runtime_defaults


class _FakeReader:
    def __init__(self) -> None:
        self.closed = False

    def editable_core_points(self):
        return [
            {
                "index": 0,
                "voltage_uv": 900_000,
                "base_freq_khz": 2_500_000,
                "current_offset_khz": 0,
            }
        ]

    def refresh_points(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def test_reset_runtime_defaults_skips_mobile_power_limit_setter(monkeypatch) -> None:
    logs: list[str] = []
    controllers = []
    readers = []

    class FakePolicyController:
        def __init__(self, *, gpu_index: int) -> None:
            self.gpu_index = int(gpu_index)
            self.power_limit_calls: list[int] = []
            self.closed = False
            controllers.append(self)

        def query_power_limits(self):
            raise AssertionError("mobile fixed power-limit getters must not run")

        def query_gpu_name(self):
            return "NVIDIA GeForce RTX 5060"

        def query_pci_device_id(self):
            return "0x2D1910DE"

        def get_clock_offsets(self):
            return {
                "gpc_clk_vf_offset_mhz": 0,
                "mem_clk_vf_offset_mhz": 0,
            }

        def reset_locked_core_clocks(self):
            return True

        def reset_locked_memory_clocks(self):
            return True

        def apply_clock_offsets(self, **kwargs):
            return dict(kwargs)

        def apply_power_limit_w(self, power_limit_w):
            self.power_limit_calls.append(int(power_limit_w))
            raise AssertionError("mobile default reset must not write power limit")

        def close(self) -> None:
            self.closed = True

    def fake_reader_factory(*, gpu_index):
        reader = _FakeReader()
        readers.append(reader)
        return reader

    monkeypatch.setattr(
        nvidia_runtime_defaults,
        "create_hidden_vf_curve_reader",
        fake_reader_factory,
    )
    monkeypatch.setattr(
        nvidia_runtime_defaults,
        "NvmlGpuPolicyController",
        FakePolicyController,
    )
    monkeypatch.setattr(nvidia_runtime_defaults, "apply_plan", lambda *_args: None)

    result = nvidia_runtime_defaults.reset_nvidia_runtime_defaults(
        gpu_index=0,
        log=logs.append,
    )

    assert controllers[0].power_limit_calls == []
    assert controllers[0].closed is True
    assert readers[0].closed is True
    assert result["pci_device_id"] == "0x2D1910DE"
    assert result["power_limit_w"] is None
    assert result["power_limits"] == {}
    assert "Reset defaults: fixed power-limit reset skipped on mobile GPU" in logs
