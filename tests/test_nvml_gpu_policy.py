from __future__ import annotations

import pytest

from drivers.nvidia.nvml_gpu_policy import fixed_power_limit_excluded_by_identity


@pytest.mark.parametrize(
    "gpu_name",
    [
        "NVIDIA GeForce RTX 5060 Laptop GPU",
        "NVIDIA GeForce RTX 4070 Mobile",
        "NVIDIA GeForce RTX 3080 Notebook GPU",
        "NVIDIA GeForce RTX 2080 with Max-Q Design",
    ],
)
def test_fixed_power_limit_excluded_for_mobile_gpu_names(gpu_name: str) -> None:
    assert fixed_power_limit_excluded_by_identity(gpu_name=gpu_name) is True


@pytest.mark.parametrize(
    "pci_device_id",
    [
        "0x2D1910DE",
        "10de:2d19",
        "10DE-2D59",
        "2D58",
    ],
)
def test_fixed_power_limit_excluded_for_mobile_pci_ids(pci_device_id: str) -> None:
    assert (
        fixed_power_limit_excluded_by_identity(
            gpu_name="NVIDIA GeForce RTX 5060",
            pci_device_id=pci_device_id,
        )
        is True
    )


def test_fixed_power_limit_allowed_for_desktop_identity() -> None:
    assert (
        fixed_power_limit_excluded_by_identity(
            gpu_name="NVIDIA GeForce RTX 5080",
            pci_device_id="0x2C0210DE",
        )
        is False
    )
