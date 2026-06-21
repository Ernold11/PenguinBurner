"""Public NVML clock telemetry helpers."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass


NVML_SUCCESS = 0
NVML_CLOCK_GRAPHICS = 0
NVML_CLOCK_SM = 1
NVML_CLOCK_MEM = 2
NVML_CLOCK_VIDEO = 3


@dataclass(frozen=True, slots=True)
class NvmlClockTelemetry:
    graphics_clock_mhz: int | None = None
    sm_clock_mhz: int | None = None
    memory_clock_mhz: int | None = None
    video_clock_mhz: int | None = None


class NvmlClockSession:
    def __init__(self, gpu_index: int, *, nvml_library=None):
        self.gpu_index = int(gpu_index)
        self._nvml = (
            nvml_library
            if nvml_library is not None
            else ctypes.CDLL("libnvidia-ml.so.1")
        )
        self._device = ctypes.c_void_p()
        self._initialized = False
        self._bind_functions()
        self._initialize_session()

    def _bind_functions(self) -> None:
        c_uint = ctypes.c_uint
        c_void_p = ctypes.c_void_p
        u_out = ctypes.POINTER(c_uint)

        self._nvml.nvmlInit_v2.restype = ctypes.c_int
        self._nvml.nvmlShutdown.restype = ctypes.c_int
        self._nvml.nvmlDeviceGetHandleByIndex_v2.argtypes = [
            c_uint,
            ctypes.POINTER(c_void_p),
        ]
        self._nvml.nvmlDeviceGetHandleByIndex_v2.restype = ctypes.c_int
        self._nvml.nvmlDeviceGetClockInfo.argtypes = [c_void_p, c_uint, u_out]
        self._nvml.nvmlDeviceGetClockInfo.restype = ctypes.c_int

        if hasattr(self._nvml, "nvmlDeviceGetSupportedMemoryClocks"):
            self._nvml.nvmlDeviceGetSupportedMemoryClocks.argtypes = [
                c_void_p,
                u_out,
                u_out,
            ]
            self._nvml.nvmlDeviceGetSupportedMemoryClocks.restype = ctypes.c_int
        if hasattr(self._nvml, "nvmlDeviceGetSupportedGraphicsClocks"):
            self._nvml.nvmlDeviceGetSupportedGraphicsClocks.argtypes = [
                c_void_p,
                c_uint,
                u_out,
                u_out,
            ]
            self._nvml.nvmlDeviceGetSupportedGraphicsClocks.restype = ctypes.c_int

    def _initialize_session(self) -> None:
        _check(self._nvml.nvmlInit_v2(), "nvmlInit_v2")
        self._initialized = True
        try:
            _check(
                self._nvml.nvmlDeviceGetHandleByIndex_v2(
                    ctypes.c_uint(self.gpu_index),
                    ctypes.byref(self._device),
                ),
                "nvmlDeviceGetHandleByIndex_v2",
            )
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self._initialized:
            self._nvml.nvmlShutdown()
            self._initialized = False

    def __enter__(self) -> NvmlClockSession:
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def current_clocks(self) -> NvmlClockTelemetry:
        return NvmlClockTelemetry(
            graphics_clock_mhz=self.clock_info_mhz(NVML_CLOCK_GRAPHICS),
            sm_clock_mhz=self.clock_info_mhz(NVML_CLOCK_SM),
            memory_clock_mhz=self.clock_info_mhz(NVML_CLOCK_MEM),
            video_clock_mhz=self.clock_info_mhz(NVML_CLOCK_VIDEO),
        )

    def clock_info_mhz(self, clock_type: int) -> int | None:
        clock_mhz = ctypes.c_uint()
        rc = int(
            self._nvml.nvmlDeviceGetClockInfo(
                self._device,
                ctypes.c_uint(int(clock_type)),
                ctypes.byref(clock_mhz),
            )
        )
        if rc != NVML_SUCCESS:
            return None
        return int(clock_mhz.value)

    def supported_memory_clocks_mhz(self) -> list[int]:
        return self._read_clock_list("nvmlDeviceGetSupportedMemoryClocks", capacity=64)

    def supported_graphics_clocks_mhz(self, memory_clock_mhz: int) -> list[int]:
        return self._read_clock_list(
            "nvmlDeviceGetSupportedGraphicsClocks",
            ctypes.c_uint(int(memory_clock_mhz)),
            capacity=512,
        )

    def supported_graphics_clock_steps_mhz(self) -> list[int]:
        steps: set[int] = set()
        for memory_clock_mhz in self.supported_memory_clocks_mhz():
            steps.update(self.supported_graphics_clocks_mhz(memory_clock_mhz))
        return sorted(steps)

    def _read_clock_list(
        self,
        getter_name: str,
        *getter_args,
        capacity: int,
    ) -> list[int]:
        getter = getattr(self._nvml, getter_name, None)
        if getter is None:
            return []

        count = ctypes.c_uint(int(capacity))
        values = (ctypes.c_uint * int(capacity))()
        rc = int(getter(self._device, *getter_args, ctypes.byref(count), values))
        if rc != NVML_SUCCESS:
            return []
        return [int(values[index]) for index in range(min(int(count.value), capacity))]


def query_nvml_current_clocks(gpu_index: int) -> NvmlClockTelemetry | None:
    try:
        with NvmlClockSession(int(gpu_index)) as session:
            return session.current_clocks()
    except Exception:
        return None


def query_nvml_supported_memory_clocks(gpu_index: int) -> list[int]:
    try:
        with NvmlClockSession(int(gpu_index)) as session:
            return session.supported_memory_clocks_mhz()
    except Exception:
        return []


def query_nvml_supported_graphics_clock_steps(gpu_index: int) -> list[int]:
    try:
        with NvmlClockSession(int(gpu_index)) as session:
            return session.supported_graphics_clock_steps_mhz()
    except Exception:
        return []


def _check(rc, name: str) -> None:
    if int(rc) != NVML_SUCCESS:
        raise RuntimeError(f"{name} failed with NVML error {int(rc)}")
