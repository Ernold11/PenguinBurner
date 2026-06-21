from __future__ import annotations

from drivers.nvidia.nvml_clock import (
    NVML_CLOCK_GRAPHICS,
    NVML_CLOCK_MEM,
    NVML_CLOCK_SM,
    NVML_CLOCK_VIDEO,
    NvmlClockSession,
)
from drivers.nvidia.nvml_power import NvmlPowerSession


class FakeNvmlFunction:
    def __init__(self, impl):
        self.impl = impl

    def __call__(self, *args):
        return self.impl(*args)


class FakeClockNvmlLibrary:
    def __init__(self):
        self.shutdown_called = False
        self.nvmlInit_v2 = FakeNvmlFunction(lambda: 0)
        self.nvmlShutdown = FakeNvmlFunction(self._shutdown)
        self.nvmlDeviceGetHandleByIndex_v2 = FakeNvmlFunction(self._handle_by_index)
        self.nvmlDeviceGetClockInfo = FakeNvmlFunction(self._clock_info)
        self.nvmlDeviceGetSupportedMemoryClocks = FakeNvmlFunction(
            self._memory_clocks
        )
        self.nvmlDeviceGetSupportedGraphicsClocks = FakeNvmlFunction(
            self._graphics_clocks
        )

    def _shutdown(self):
        self.shutdown_called = True
        return 0

    def _handle_by_index(self, _index, device_ptr):
        device_ptr._obj.value = 123
        return 0

    def _clock_info(self, _device, clock_type, clock_ptr):
        values = {
            NVML_CLOCK_GRAPHICS: 2100,
            NVML_CLOCK_SM: 2055,
            NVML_CLOCK_MEM: 10500,
            NVML_CLOCK_VIDEO: 1800,
        }
        clock_ptr._obj.value = values[int(clock_type.value)]
        return 0

    def _memory_clocks(self, _device, count_ptr, values):
        clocks = [9000, 10500]
        count_ptr._obj.value = len(clocks)
        for index, clock_mhz in enumerate(clocks):
            values[index] = clock_mhz
        return 0

    def _graphics_clocks(self, _device, memory_clock_mhz, count_ptr, values):
        clocks_by_memory = {
            9000: [1800, 1900],
            10500: [2000, 2100],
        }
        clocks = clocks_by_memory[int(memory_clock_mhz.value)]
        count_ptr._obj.value = len(clocks)
        for index, clock_mhz in enumerate(clocks):
            values[index] = clock_mhz
        return 0


class FakePowerNvmlLibrary:
    def __init__(self):
        self.shutdown_called = False
        self.nvmlInit_v2 = FakeNvmlFunction(lambda: 0)
        self.nvmlShutdown = FakeNvmlFunction(self._shutdown)
        self.nvmlDeviceGetHandleByIndex_v2 = FakeNvmlFunction(self._handle_by_index)
        self.nvmlDeviceGetPowerUsage = FakeNvmlFunction(
            lambda _device, out: self._write(out, 286_500)
        )
        self.nvmlDeviceGetPowerManagementMode = FakeNvmlFunction(
            lambda _device, out: self._write(out, 1)
        )
        self.nvmlDeviceGetPowerManagementLimit = FakeNvmlFunction(
            lambda _device, out: self._write(out, 320_000)
        )
        self.nvmlDeviceGetEnforcedPowerLimit = FakeNvmlFunction(
            lambda _device, out: self._write(out, 310_000)
        )
        self.nvmlDeviceGetPowerManagementDefaultLimit = FakeNvmlFunction(
            lambda _device, out: self._write(out, 350_000)
        )
        self.nvmlDeviceGetPowerManagementLimitConstraints = FakeNvmlFunction(
            self._constraints
        )

    def _shutdown(self):
        self.shutdown_called = True
        return 0

    def _handle_by_index(self, _index, device_ptr):
        device_ptr._obj.value = 123
        return 0

    def _write(self, out, value):
        out._obj.value = value
        return 0

    def _constraints(self, _device, min_ptr, max_ptr):
        min_ptr._obj.value = 200_000
        max_ptr._obj.value = 450_000
        return 0


def test_nvml_clock_session_reads_current_and_supported_clocks() -> None:
    fake_nvml = FakeClockNvmlLibrary()

    with NvmlClockSession(0, nvml_library=fake_nvml) as session:
        telemetry = session.current_clocks()
        assert telemetry.graphics_clock_mhz == 2100
        assert telemetry.sm_clock_mhz == 2055
        assert telemetry.memory_clock_mhz == 10500
        assert telemetry.video_clock_mhz == 1800
        assert session.supported_memory_clocks_mhz() == [9000, 10500]
        assert session.supported_graphics_clock_steps_mhz() == [
            1800,
            1900,
            2000,
            2100,
        ]

    assert fake_nvml.shutdown_called is True


def test_nvml_power_session_reads_draw_and_limit_telemetry() -> None:
    fake_nvml = FakePowerNvmlLibrary()

    with NvmlPowerSession(0, nvml_library=fake_nvml) as session:
        telemetry = session.telemetry()
        assert telemetry.power_draw_w == 286.5
        assert telemetry.power_management_enabled is True
        assert telemetry.power_limit_w == 320.0
        assert telemetry.enforced_power_limit_w == 310.0
        assert telemetry.power_limit_default_w == 350.0
        assert telemetry.power_limit_min_w == 200.0
        assert telemetry.power_limit_max_w == 450.0

    assert fake_nvml.shutdown_called is True
