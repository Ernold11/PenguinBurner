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


def test_reset_runtime_defaults_passes_through_mobile_daemon_result(monkeypatch) -> None:
    # The reset delegates to the root daemon, which handles mobile GPUs
    # safely and returns no settable power limit; the Python side must pass
    # that through as power_limit_w=None without erroring (0.6.6 mobile).
    logs: list[str] = []

    def fake_gpu_reset_defaults(gpu_index):
        assert gpu_index == 0
        return {
            "gpu_name": "NVIDIA GeForce RTX 5060 Laptop GPU",
            "pci_device_id": "0x2D1910DE",
            "points": [
                {
                    "index": 0,
                    "type": 0,
                    "voltage_based": 1,
                    "voltage_uv": 900_000,
                    "base_freq_khz": 2_500_000,
                    "current_offset_khz": 0,
                },
            ],
            "power_limits": {},
        }

    monkeypatch.setattr(
        nvidia_runtime_defaults, "gpu_reset_defaults", fake_gpu_reset_defaults
    )

    result = nvidia_runtime_defaults.reset_nvidia_runtime_defaults(
        gpu_index=0,
        log=logs.append,
    )

    assert result["gpu_name"] == "NVIDIA GeForce RTX 5060 Laptop GPU"
    assert result["pci_device_id"] == "0x2D1910DE"
    assert result["power_limit_w"] is None
    assert result["power_limits"] == {}
    assert len(result["plan"]) == 1
    assert any("Reset defaults through penguin-burnerd" in line for line in logs)
