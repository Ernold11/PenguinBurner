"""Public NVML power telemetry helpers."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass


NVML_SUCCESS = 0
NVML_FEATURE_ENABLED = 1


@dataclass(frozen=True, slots=True)
class NvmlPowerTelemetry:
    power_draw_w: float | None = None
    power_management_enabled: bool | None = None
    power_limit_w: float | None = None
    enforced_power_limit_w: float | None = None
    power_limit_default_w: float | None = None
    power_limit_min_w: float | None = None
    power_limit_max_w: float | None = None


class NvmlPowerSession:
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

        for name in (
            "nvmlDeviceGetPowerUsage",
            "nvmlDeviceGetPowerManagementMode",
            "nvmlDeviceGetPowerManagementLimit",
            "nvmlDeviceGetEnforcedPowerLimit",
            "nvmlDeviceGetPowerManagementDefaultLimit",
        ):
            getter = getattr(self._nvml, name, None)
            if getter is not None:
                getter.argtypes = [c_void_p, u_out]
                getter.restype = ctypes.c_int

        getter = getattr(
            self._nvml,
            "nvmlDeviceGetPowerManagementLimitConstraints",
            None,
        )
        if getter is not None:
            getter.argtypes = [c_void_p, u_out, u_out]
            getter.restype = ctypes.c_int

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

    def __enter__(self) -> NvmlPowerSession:
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def telemetry(self) -> NvmlPowerTelemetry:
        min_limit_w, max_limit_w = self.power_limit_constraints_w()
        return NvmlPowerTelemetry(
            power_draw_w=self.power_draw_w(),
            power_management_enabled=self.power_management_enabled(),
            power_limit_w=self.power_value_w("nvmlDeviceGetPowerManagementLimit"),
            enforced_power_limit_w=self.power_value_w(
                "nvmlDeviceGetEnforcedPowerLimit"
            ),
            power_limit_default_w=self.power_value_w(
                "nvmlDeviceGetPowerManagementDefaultLimit"
            ),
            power_limit_min_w=min_limit_w,
            power_limit_max_w=max_limit_w,
        )

    def power_draw_w(self) -> float | None:
        return self.power_value_w("nvmlDeviceGetPowerUsage")

    def power_management_enabled(self) -> bool | None:
        value = self._read_uint("nvmlDeviceGetPowerManagementMode")
        if value is None:
            return None
        return int(value) == NVML_FEATURE_ENABLED

    def power_value_w(self, getter_name: str) -> float | None:
        value_mw = self._read_uint(getter_name)
        if value_mw is None:
            return None
        return float(value_mw) / 1000.0

    def power_limit_constraints_w(self) -> tuple[float | None, float | None]:
        getter = getattr(
            self._nvml,
            "nvmlDeviceGetPowerManagementLimitConstraints",
            None,
        )
        if getter is None:
            return None, None

        min_limit_mw = ctypes.c_uint()
        max_limit_mw = ctypes.c_uint()
        rc = int(
            getter(
                self._device,
                ctypes.byref(min_limit_mw),
                ctypes.byref(max_limit_mw),
            )
        )
        if rc != NVML_SUCCESS:
            return None, None
        return float(min_limit_mw.value) / 1000.0, float(max_limit_mw.value) / 1000.0

    def _read_uint(self, getter_name: str) -> int | None:
        getter = getattr(self._nvml, getter_name, None)
        if getter is None:
            return None

        out = ctypes.c_uint()
        rc = int(getter(self._device, ctypes.byref(out)))
        if rc != NVML_SUCCESS:
            return None
        return int(out.value)


def query_nvml_power_telemetry(gpu_index: int) -> NvmlPowerTelemetry | None:
    try:
        with NvmlPowerSession(int(gpu_index)) as session:
            return session.telemetry()
    except Exception:
        return None


def query_nvml_power_draw_w(gpu_index: int) -> float | None:
    telemetry = query_nvml_power_telemetry(int(gpu_index))
    if telemetry is None:
        return None
    return telemetry.power_draw_w


def _check(rc, name: str) -> None:
    if int(rc) != NVML_SUCCESS:
        raise RuntimeError(f"{name} failed with NVML error {int(rc)}")
