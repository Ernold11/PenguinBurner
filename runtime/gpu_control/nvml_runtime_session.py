from __future__ import annotations

import ctypes

from .live_gpu_telemetry_text import (
    format_telemetry as format_live_gpu_telemetry,
    get_power_draw_w,
)
from .nvml_return_code import (
    NVML_SUCCESS,
    NVML_TEMPERATURE_GPU,
    check_nvml_return_code,
)


class NvmlRuntimeSession:
    def __init__(self, gpu_index: int):
        self.gpu_index = int(gpu_index)
        self._nvml = ctypes.CDLL("libnvidia-ml.so.1")
        self._device = ctypes.c_void_p()
        self._initialized = False
        self._bind_functions()
        self._initialize_session()

    def _bind_functions(self) -> None:
        u, dev = ctypes.c_uint, ctypes.c_void_p
        u_out = ctypes.POINTER(ctypes.c_uint)
        # (name, argtypes, optional)
        bindings = [
            ("nvmlInit_v2", [], False),
            ("nvmlShutdown", [], False),
            ("nvmlDeviceGetHandleByIndex_v2", [u, ctypes.POINTER(dev)], False),
            ("nvmlDeviceGetTemperature", [dev, u, u_out], False),
            ("nvmlDeviceGetNumFans", [dev, u_out], False),
            ("nvmlDeviceGetFanSpeed", [dev, u_out], False),
            ("nvmlDeviceGetFanSpeed_v2", [dev, u, u_out], False),
            ("nvmlDeviceGetPowerUsage", [dev, u_out], False),
            ("nvmlDeviceGetClockInfo", [dev, u, u_out], False),
            ("nvmlDeviceGetMinMaxFanSpeed", [dev, u_out, u_out], True),
            ("nvmlDeviceSetFanSpeed_v2", [dev, u, u], True),
            ("nvmlDeviceSetDefaultFanSpeed_v2", [dev, u], True),
        ]
        for name, argtypes, optional in bindings:
            fn = getattr(self._nvml, name, None)
            if fn is None:
                if optional:
                    continue
                raise AttributeError(f"libnvidia-ml is missing required symbol {name}")
            fn.argtypes = argtypes
            fn.restype = ctypes.c_int

    def _initialize_session(self) -> None:
        check_nvml_return_code(self._nvml.nvmlInit_v2(), "nvmlInit_v2")
        self._initialized = True
        try:
            check_nvml_return_code(
                self._nvml.nvmlDeviceGetHandleByIndex_v2(
                    ctypes.c_uint(self.gpu_index),
                    ctypes.byref(self._device),
                ),
                "nvmlDeviceGetHandleByIndex_v2",
            )
        except Exception:
            self.close()
            raise

    @property
    def nvml(self):
        return self._nvml

    @property
    def device(self):
        return self._device

    def close(self) -> None:
        if self._initialized:
            self._nvml.nvmlShutdown()
            self._initialized = False

    def fan_count(self) -> int:
        count = ctypes.c_uint()
        check_nvml_return_code(
            self._nvml.nvmlDeviceGetNumFans(self._device, ctypes.byref(count)),
            "nvmlDeviceGetNumFans",
        )
        return int(count.value)

    def fan_speed_limits(self) -> tuple[int | None, int | None]:
        getter = getattr(self._nvml, "nvmlDeviceGetMinMaxFanSpeed", None)
        if getter is None:
            return None, None

        fan_min = ctypes.c_uint()
        fan_max = ctypes.c_uint()
        rc = int(getter(self._device, ctypes.byref(fan_min), ctypes.byref(fan_max)))
        if rc == NVML_SUCCESS and fan_max.value >= fan_min.value:
            return int(fan_min.value), int(fan_max.value)
        return None, None

    def set_default_fan_speed(self, fan_idx: int) -> None:
        setter = getattr(self._nvml, "nvmlDeviceSetDefaultFanSpeed_v2", None)
        if setter is None:
            raise RuntimeError(
                "nvmlDeviceSetDefaultFanSpeed_v2 is not available on this system"
            )
        check_nvml_return_code(
            setter(self._device, ctypes.c_uint(int(fan_idx))),
            f"nvmlDeviceSetDefaultFanSpeed_v2 fan {int(fan_idx)}",
        )

    def set_all_fans_default(self, fan_count: int) -> None:
        for fan_idx in range(int(fan_count)):
            self.set_default_fan_speed(fan_idx)

    def set_fan_speed(self, fan_idx: int, speed_pct: int) -> None:
        setter = getattr(self._nvml, "nvmlDeviceSetFanSpeed_v2", None)
        if setter is None:
            raise RuntimeError("nvmlDeviceSetFanSpeed_v2 is not available on this system")
        check_nvml_return_code(
            setter(
                self._device,
                ctypes.c_uint(int(fan_idx)),
                ctypes.c_uint(int(speed_pct)),
            ),
            f"nvmlDeviceSetFanSpeed_v2 fan {int(fan_idx)}",
        )

    def set_all_fans_speed(self, fan_count: int, speed_pct: int) -> None:
        for fan_idx in range(int(fan_count)):
            self.set_fan_speed(fan_idx, speed_pct)

    def temperature_c(self) -> float:
        temp = ctypes.c_uint()
        check_nvml_return_code(
            self._nvml.nvmlDeviceGetTemperature(
                self._device,
                ctypes.c_uint(NVML_TEMPERATURE_GPU),
                ctypes.byref(temp),
            ),
            "nvmlDeviceGetTemperature",
        )
        return float(temp.value)

    def power_draw_w(self) -> float | None:
        return get_power_draw_w(self._nvml, self._device)

    def format_telemetry(
        self,
        *,
        fan_count: int,
        current_temp_c: float,
        voltage_reader=None,
        vf_curve_reader=None,
        gpu_policy_controller=None,
        power_draw_w=None,
        clock_ceiling_controller=None,
    ) -> str:
        return format_live_gpu_telemetry(
            self._nvml,
            self._device,
            int(fan_count),
            current_temp_c,
            voltage_reader=voltage_reader,
            vf_curve_reader=vf_curve_reader,
            gpu_policy_controller=gpu_policy_controller,
            power_draw_w=power_draw_w,
            clock_ceiling_controller=clock_ceiling_controller,
        )
