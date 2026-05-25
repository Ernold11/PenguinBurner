"""Read informational NVML clock throttle reason state."""

from __future__ import annotations

import ctypes
from collections.abc import Iterable


NVML_SUCCESS = 0
NVML_CLOCKS_THROTTLE_REASON_GPU_IDLE = 0x0000000000000001
NVML_CLOCKS_THROTTLE_REASON_APPLICATIONS_CLOCKS_SETTING = 0x0000000000000002
NVML_CLOCKS_THROTTLE_REASON_SW_POWER_CAP = 0x0000000000000004
NVML_CLOCKS_THROTTLE_REASON_HW_SLOWDOWN = 0x0000000000000008
NVML_CLOCKS_THROTTLE_REASON_SYNC_BOOST = 0x0000000000000010
NVML_CLOCKS_THROTTLE_REASON_SW_THERMAL_SLOWDOWN = 0x0000000000000020
NVML_CLOCKS_THROTTLE_REASON_HW_THERMAL_SLOWDOWN = 0x0000000000000040
NVML_CLOCKS_THROTTLE_REASON_HW_POWER_BRAKE_SLOWDOWN = 0x0000000000000080
NVML_CLOCKS_THROTTLE_REASON_DISPLAY_CLOCK_SETTING = 0x0000000000000100


_REASON_BITS: tuple[tuple[int, str], ...] = (
    (NVML_CLOCKS_THROTTLE_REASON_GPU_IDLE, "idle"),
    (NVML_CLOCKS_THROTTLE_REASON_APPLICATIONS_CLOCKS_SETTING, "app-clocks"),
    (NVML_CLOCKS_THROTTLE_REASON_SW_POWER_CAP, "sw-power"),
    (NVML_CLOCKS_THROTTLE_REASON_HW_SLOWDOWN, "hw-slowdown"),
    (NVML_CLOCKS_THROTTLE_REASON_SYNC_BOOST, "sync-boost"),
    (NVML_CLOCKS_THROTTLE_REASON_SW_THERMAL_SLOWDOWN, "sw-thermal"),
    (NVML_CLOCKS_THROTTLE_REASON_HW_THERMAL_SLOWDOWN, "hw-thermal"),
    (NVML_CLOCKS_THROTTLE_REASON_HW_POWER_BRAKE_SLOWDOWN, "hw-power-brake"),
    (NVML_CLOCKS_THROTTLE_REASON_DISPLAY_CLOCK_SETTING, "display-clock"),
)


def decode_perf_cap_reason_mask(mask: int) -> tuple[str, ...]:
    """Decode NVML clocks throttle reason bits into compact UI labels."""

    mask_value = int(mask)
    if mask_value == 0:
        return ("none",)

    known_mask = 0
    labels: list[str] = []
    for bit, label in _REASON_BITS:
        known_mask |= int(bit)
        if mask_value & int(bit):
            labels.append(label)

    unknown_mask = mask_value & ~known_mask
    if unknown_mask:
        labels.append(f"unknown-0x{unknown_mask:x}")
    return tuple(labels) or ("none",)


def format_perf_cap_reasons(reasons: Iterable[str]) -> str:
    labels = [str(reason).strip() for reason in reasons if str(reason).strip()]
    return "+".join(labels) if labels else "none"


def format_perf_cap_reason_mask(mask: int) -> str:
    return format_perf_cap_reasons(decode_perf_cap_reason_mask(mask))


class NvmlPerfCapReasonReader:
    """Small NVML session for reading current clock throttle reasons."""

    def __init__(self, gpu_index: int, *, nvml_library=None):
        self.gpu_index = int(gpu_index)
        self._nvml = (
            nvml_library
            if nvml_library is not None
            else ctypes.CDLL("libnvidia-ml.so.1")
        )
        self._device = ctypes.c_void_p()
        self._initialized = False
        self._get_current_reasons = None
        try:
            self._bind_functions()
            self._initialize_session()
        except Exception:
            self.close()
            raise

    def _bind_functions(self) -> None:
        c_uint = ctypes.c_uint
        c_void_p = ctypes.c_void_p
        c_ulonglong = ctypes.c_ulonglong

        self._nvml.nvmlInit_v2.restype = ctypes.c_int
        self._nvml.nvmlShutdown.restype = ctypes.c_int
        self._nvml.nvmlDeviceGetHandleByIndex_v2.argtypes = [
            c_uint,
            ctypes.POINTER(c_void_p),
        ]
        self._nvml.nvmlDeviceGetHandleByIndex_v2.restype = ctypes.c_int

        getter = self._current_reason_getter()
        if getter is None:
            raise RuntimeError(
                "NVML current clock throttle reason query is not available"
            )
        getter.argtypes = [c_void_p, ctypes.POINTER(c_ulonglong)]
        getter.restype = ctypes.c_int
        self._get_current_reasons = getter

    def _current_reason_getter(self):
        for name in (
            "nvmlDeviceGetCurrentClocksThrottleReasons",
            "nvmlDeviceGetCurrentClocksEventReasons",
        ):
            getter = getattr(self._nvml, name, None)
            if getter is not None:
                return getter
        return None

    def _initialize_session(self) -> None:
        _check_nvml_return_code(self._nvml.nvmlInit_v2(), "nvmlInit_v2")
        self._initialized = True
        try:
            _check_nvml_return_code(
                self._nvml.nvmlDeviceGetHandleByIndex_v2(
                    ctypes.c_uint(self.gpu_index),
                    ctypes.byref(self._device),
                ),
                "nvmlDeviceGetHandleByIndex_v2",
            )
        except Exception:
            self.close()
            raise

    def read_mask(self) -> int | None:
        getter = self._get_current_reasons
        if getter is None:
            return None

        mask = ctypes.c_ulonglong()
        rc = int(getter(self._device, ctypes.byref(mask)))
        if rc != NVML_SUCCESS:
            return None
        return int(mask.value)

    def read_reason(self) -> str | None:
        mask = self.read_mask()
        if mask is None:
            return None
        return format_perf_cap_reason_mask(mask)

    def close(self) -> None:
        if self._initialized:
            self._nvml.nvmlShutdown()
            self._initialized = False


def _check_nvml_return_code(rc, name: str) -> None:
    if int(rc) != NVML_SUCCESS:
        raise RuntimeError(f"{name} failed with NVML error {int(rc)}")
