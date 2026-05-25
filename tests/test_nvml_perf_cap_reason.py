from __future__ import annotations

from nvml_perf_cap_reason import (
    NVML_CLOCKS_THROTTLE_REASON_HW_POWER_BRAKE_SLOWDOWN,
    NVML_CLOCKS_THROTTLE_REASON_SW_POWER_CAP,
    NvmlPerfCapReasonReader,
    decode_perf_cap_reason_mask,
    format_perf_cap_reason_mask,
)


class FakeNvmlFunction:
    def __init__(self, impl):
        self.impl = impl

    def __call__(self, *args):
        return self.impl(*args)


class FakeNvmlLibrary:
    def __init__(self, reason_mask: int):
        self.reason_mask = int(reason_mask)
        self.shutdown_called = False
        self.nvmlInit_v2 = FakeNvmlFunction(lambda: 0)
        self.nvmlShutdown = FakeNvmlFunction(self._shutdown)
        self.nvmlDeviceGetHandleByIndex_v2 = FakeNvmlFunction(self._handle_by_index)
        self.nvmlDeviceGetCurrentClocksThrottleReasons = FakeNvmlFunction(
            self._current_reasons
        )

    def _shutdown(self):
        self.shutdown_called = True
        return 0

    def _handle_by_index(self, _index, device_ptr):
        device_ptr._obj.value = 123
        return 0

    def _current_reasons(self, _device, reason_ptr):
        reason_ptr._obj.value = self.reason_mask
        return 0


def test_decode_perf_cap_reason_mask_formats_known_and_unknown_bits() -> None:
    mask = (
        NVML_CLOCKS_THROTTLE_REASON_SW_POWER_CAP
        | NVML_CLOCKS_THROTTLE_REASON_HW_POWER_BRAKE_SLOWDOWN
        | 0x8000
    )

    assert decode_perf_cap_reason_mask(mask) == (
        "sw-power",
        "hw-power-brake",
        "unknown-0x8000",
    )
    assert format_perf_cap_reason_mask(0) == "none"


def test_nvml_perf_cap_reason_reader_reads_current_reason_mask() -> None:
    fake_nvml = FakeNvmlLibrary(NVML_CLOCKS_THROTTLE_REASON_SW_POWER_CAP)

    reader = NvmlPerfCapReasonReader(0, nvml_library=fake_nvml)
    try:
        assert reader.read_mask() == NVML_CLOCKS_THROTTLE_REASON_SW_POWER_CAP
        assert reader.read_reason() == "sw-power"
    finally:
        reader.close()

    assert fake_nvml.shutdown_called is True
