from __future__ import annotations

import pytest

from drivers.nvidia.nvml_gpu_policy import fixed_power_limit_excluded_by_identity
from drivers.nvidia.nvml_gpu_policy import power_limit_setter_probe_risky


@pytest.mark.parametrize(
    "gpu_name",
    [
        "NVIDIA GeForce RTX 5060 Laptop GPU",
        "NVIDIA GeForce RTX 4070 Mobile",
        "NVIDIA GeForce RTX 3080 Notebook GPU",
        "NVIDIA GeForce RTX 2080 with Max-Q Design",
    ],
)
def test_power_limit_setter_probe_skips_mobile_gpu_names(gpu_name: str) -> None:
    assert power_limit_setter_probe_risky(gpu_name=gpu_name, power_limits={}) is True


@pytest.mark.parametrize(
    "pci_device_id",
    [
        "0x2D1910DE",
        "10de:2d19",
        "10DE-2D59",
        "2D58",
    ],
)
def test_power_limit_setter_probe_skips_mobile_pci_ids(
    pci_device_id: str,
) -> None:
    assert (
        power_limit_setter_probe_risky(
            gpu_name="NVIDIA GeForce RTX 5060",
            pci_device_id=pci_device_id,
            power_limits={},
        )
        is True
    )
    assert (
        fixed_power_limit_excluded_by_identity(
            gpu_name="NVIDIA GeForce RTX 5060",
            pci_device_id=pci_device_id,
        )
        is True
    )


def test_power_limit_setter_probe_allows_desktop_class_limits() -> None:
    assert (
        power_limit_setter_probe_risky(
            gpu_name="NVIDIA GeForce RTX 5080",
            power_limits={
                "power_limit_w": 360,
                "power_limit_default_w": 360,
                "power_limit_min_w": 300,
                "power_limit_max_w": 390,
            },
        )
        is False
    )
