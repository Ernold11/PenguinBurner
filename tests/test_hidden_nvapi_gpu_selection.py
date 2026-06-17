from __future__ import annotations

from nvidia_driver import hidden_nvapi_gpu_selection as selection


def test_pci_bus_number_from_full_nvml_bus_id() -> None:
    assert selection.pci_bus_number_from_bus_id("00000000:2B:00.0") == 0x2B


def test_pci_bus_number_from_legacy_bus_id() -> None:
    assert selection.pci_bus_number_from_bus_id("03:00.0") == 0x03


def test_pci_bus_number_from_invalid_bus_id() -> None:
    assert selection.pci_bus_number_from_bus_id("") is None
    assert selection.pci_bus_number_from_bus_id("not-a-bus") is None
