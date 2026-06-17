"""Public NVML telemetry sampling for Q2RTX stability runs."""

from __future__ import annotations

import ctypes

from runtime_gpu_control.nvml_return_code import (
    NVML_CLOCK_GRAPHICS,
    NVML_SUCCESS,
    NVML_TEMPERATURE_GPU,
)

from .models import TelemetrySample


class NvmlUtilization(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class NvmlTelemetrySession:
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
        self._nvml.nvmlDeviceGetUtilizationRates.argtypes = [
            c_void_p,
            ctypes.POINTER(NvmlUtilization),
        ]
        self._nvml.nvmlDeviceGetUtilizationRates.restype = ctypes.c_int
        self._nvml.nvmlDeviceGetPowerUsage.argtypes = [c_void_p, u_out]
        self._nvml.nvmlDeviceGetPowerUsage.restype = ctypes.c_int
        self._nvml.nvmlDeviceGetClockInfo.argtypes = [c_void_p, c_uint, u_out]
        self._nvml.nvmlDeviceGetClockInfo.restype = ctypes.c_int
        self._nvml.nvmlDeviceGetTemperature.argtypes = [c_void_p, c_uint, u_out]
        self._nvml.nvmlDeviceGetTemperature.restype = ctypes.c_int
        if hasattr(self._nvml, "nvmlDeviceGetFanSpeed_v2"):
            self._nvml.nvmlDeviceGetFanSpeed_v2.argtypes = [c_void_p, c_uint, u_out]
            self._nvml.nvmlDeviceGetFanSpeed_v2.restype = ctypes.c_int
        if hasattr(self._nvml, "nvmlDeviceGetFanSpeed"):
            self._nvml.nvmlDeviceGetFanSpeed.argtypes = [c_void_p, u_out]
            self._nvml.nvmlDeviceGetFanSpeed.restype = ctypes.c_int

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

    def read_sample(
        self,
        *,
        elapsed_s: float = 0.0,
        voltage_mv: float | None = None,
        perf_cap_reason: str | None = None,
    ) -> TelemetrySample:
        return TelemetrySample(
            elapsed_s=float(elapsed_s),
            gpu_util_pct=self._gpu_util_pct(),
            power_w=self._power_w(),
            core_clock_mhz=self._core_clock_mhz(),
            temperature_c=self._temperature_c(),
            voltage_mv=voltage_mv,
            fan_speed_pct=self._fan_speed_pct(),
            perf_cap_reason=perf_cap_reason,
        )

    def close(self) -> None:
        if self._initialized:
            self._nvml.nvmlShutdown()
            self._initialized = False

    def _gpu_util_pct(self) -> float | None:
        util = NvmlUtilization()
        rc = int(
            self._nvml.nvmlDeviceGetUtilizationRates(
                self._device,
                ctypes.byref(util),
            )
        )
        if rc != NVML_SUCCESS:
            return None
        return float(util.gpu)

    def _power_w(self) -> float | None:
        power_mw = ctypes.c_uint()
        rc = int(self._nvml.nvmlDeviceGetPowerUsage(self._device, ctypes.byref(power_mw)))
        if rc != NVML_SUCCESS:
            return None
        return float(power_mw.value) / 1000.0

    def _core_clock_mhz(self) -> float | None:
        clock_mhz = ctypes.c_uint()
        rc = int(
            self._nvml.nvmlDeviceGetClockInfo(
                self._device,
                ctypes.c_uint(NVML_CLOCK_GRAPHICS),
                ctypes.byref(clock_mhz),
            )
        )
        if rc != NVML_SUCCESS:
            return None
        return float(clock_mhz.value)

    def _temperature_c(self) -> float | None:
        temp_c = ctypes.c_uint()
        rc = int(
            self._nvml.nvmlDeviceGetTemperature(
                self._device,
                ctypes.c_uint(NVML_TEMPERATURE_GPU),
                ctypes.byref(temp_c),
            )
        )
        if rc != NVML_SUCCESS:
            return None
        return float(temp_c.value)

    def _fan_speed_pct(self) -> float | None:
        fan_speed = ctypes.c_uint()
        getter_v2 = getattr(self._nvml, "nvmlDeviceGetFanSpeed_v2", None)
        if getter_v2 is not None:
            rc = int(getter_v2(self._device, ctypes.c_uint(0), ctypes.byref(fan_speed)))
            if rc == NVML_SUCCESS:
                return float(fan_speed.value)
        getter = getattr(self._nvml, "nvmlDeviceGetFanSpeed", None)
        if getter is None:
            return None
        rc = int(getter(self._device, ctypes.byref(fan_speed)))
        if rc != NVML_SUCCESS:
            return None
        return float(fan_speed.value)


def create_nvml_telemetry_session(gpu_index: int) -> NvmlTelemetrySession | None:
    try:
        return NvmlTelemetrySession(int(gpu_index))
    except Exception:
        return None


def _check(rc, name: str) -> None:
    if int(rc) != NVML_SUCCESS:
        raise RuntimeError(f"{name} failed with NVML error {int(rc)}")
