#!/usr/bin/env python3

from __future__ import annotations

import ctypes
from typing import Any, cast

from drivers.nvidia.nvml_identity import NvmlPciInfo
from integrations.afterburner.policy import (
    MAX_AFTERBURNER_MEM_OFFSET_MHZ,
    afterburner_offset_khz_to_mhz,
    apply_translated_gpu_policy,
    clamp_afterburner_mem_offset_mhz,
    describe_translated_gpu_policy,
    translate_afterburner_gpu_policy,
    translate_afterburner_power_limit_pct,
)

NVML_SUCCESS = 0
NVML_FEATURE_ENABLED = 1
FIXED_POWER_LIMIT_MOBILE_NAME_TOKENS = (
    "laptop",
    "mobile",
    "notebook",
    "max-q",
    "max q",
)
FIXED_POWER_LIMIT_MOBILE_PCI_DEVICE_IDS = frozenset(
    {
        # Blackwell notebook IDs from NVIDIA supported-chip tables.
        "2BB4",
        "2C18",
        "2C19",
        "2C38",
        "2C39",
        "2C58",
        "2C59",
        "2D18",
        "2D19",
        "2D39",
        "2D58",
        "2D59",
        "2DB8",
        "2DB9",
        "2F18",
        "2F38",
        "2F58",
    }
)

__all__ = [
    "MAX_AFTERBURNER_MEM_OFFSET_MHZ",
    "NVML_SUCCESS",
    "NvmlGpuPolicyController",
    "afterburner_offset_khz_to_mhz",
    "apply_translated_gpu_policy",
    "clamp_afterburner_mem_offset_mhz",
    "describe_translated_gpu_policy",
    "driver_memory_offset_limit_mhz",
    "fixed_power_limit_excluded_by_identity",
    "power_limit_setter_probe_risky",
    "translate_afterburner_gpu_policy",
    "translate_afterburner_power_limit_pct",
]


def driver_memory_offset_limit_mhz(policy_controller=None) -> int:
    """Positive memory-offset cap for this GPU.

    The driver-reported max (``nvmlDeviceGetMemClkMinMaxVfOffset``) is the
    real authority; the static Afterburner-style cap is only the fallback
    when NVML does not expose a range.
    """
    if policy_controller is not None:
        try:
            driver_range = policy_controller.get_memory_clock_offset_range_mhz()
        except Exception:
            driver_range = None
        if driver_range:
            try:
                return max(0, int(driver_range[1]))
            except (TypeError, ValueError):
                pass
    return MAX_AFTERBURNER_MEM_OFFSET_MHZ


def power_limit_setter_probe_risky(
    *,
    gpu_name: object | None = None,
    pci_device_id: object | None = None,
    power_limits: dict | None = None,
) -> bool:
    """Avoid fixed power-limit writes on mobile/low-TGP GPUs.

    Some Nvidia notebook drivers expose readable power-limit fields or
    constraints while rejecting the fixed manual setter. On those systems
    nvidia-powerd/Dynamic Boost can be the supported power-budget path instead
    of a stable nvidia-smi -pl style cap. A no-op setter call is only a
    capability probe, not required for Auto-UV itself, so prefer disabling the
    fixed power-limit control over poking that setter on hardware that looks
    mobile or notebook-class.
    """

    if fixed_power_limit_excluded_by_identity(
        gpu_name=gpu_name,
        pci_device_id=pci_device_id,
    ):
        return True
    limits = power_limits if isinstance(power_limits, dict) else {}
    default_w = _positive_power_limit_w(limits.get("power_limit_default_w"))
    max_w = _positive_power_limit_w(limits.get("power_limit_max_w"))
    current_w = _positive_power_limit_w(limits.get("power_limit_w"))
    reference_w = default_w or current_w
    return reference_w is not None and reference_w <= 100 and (
        max_w is None or max_w <= 150
    )


def fixed_power_limit_excluded_by_identity(
    *,
    gpu_name: object | None = None,
    pci_device_id: object | None = None,
) -> bool:
    name = str(gpu_name or "").lower()
    if any(token in name for token in FIXED_POWER_LIMIT_MOBILE_NAME_TOKENS):
        return True
    return _normalize_pci_device_id(pci_device_id) in (
        FIXED_POWER_LIMIT_MOBILE_PCI_DEVICE_IDS
    )


def _normalize_pci_device_id(value: object | None) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    text = (
        text.replace("0X", "")
        .replace(":", " ")
        .replace("-", " ")
        .replace("_", " ")
    )
    parts = [
        "".join(ch for ch in part if ch in "0123456789ABCDEF")
        for part in text.split()
    ]
    parts = [part for part in parts if part]
    if len(parts) >= 2 and parts[0] == "10DE":
        return parts[1][-4:].zfill(4)
    token = parts[0] if parts else ""
    if len(token) >= 8 and token.endswith("10DE"):
        return token[:4]
    if len(token) >= 8 and token.startswith("10DE"):
        return token[4:8]
    return token[-4:].zfill(4) if token else ""


def _positive_power_limit_w(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        watts = int(round(float(cast(Any, value))))
    except (TypeError, ValueError):
        return None
    return watts if watts > 0 else None


class NvmlGpuPolicyController:
    def __init__(self, gpu_index=0):
        self._gpu_index = int(gpu_index)
        self._nvml = ctypes.CDLL("libnvidia-ml.so.1")
        self._device = ctypes.c_void_p()
        self._initialized = False
        self._bind_functions()
        self._initialize_session()

    def _bind_functions(self):
        c_uint = ctypes.c_uint
        c_int = ctypes.c_int
        c_void_p = ctypes.c_void_p

        self._nvml.nvmlInit_v2.restype = c_int
        self._nvml.nvmlShutdown.restype = c_int
        self._nvml.nvmlDeviceGetHandleByIndex_v2.argtypes = [
            c_uint,
            ctypes.POINTER(c_void_p),
        ]
        self._nvml.nvmlDeviceGetHandleByIndex_v2.restype = c_int
        if hasattr(self._nvml, "nvmlDeviceGetName"):
            self._nvml.nvmlDeviceGetName.argtypes = [
                c_void_p,
                ctypes.POINTER(ctypes.c_char),
                c_uint,
            ]
            self._nvml.nvmlDeviceGetName.restype = c_int
        for name in ("nvmlDeviceGetPciInfo_v3", "nvmlDeviceGetPciInfo_v2"):
            getter = getattr(self._nvml, name, None)
            if getter is not None:
                getter.argtypes = [c_void_p, ctypes.POINTER(NvmlPciInfo)]
                getter.restype = c_int

        if hasattr(self._nvml, "nvmlErrorString"):
            self._nvml.nvmlErrorString.argtypes = [c_int]
            self._nvml.nvmlErrorString.restype = ctypes.c_char_p

        if hasattr(self._nvml, "nvmlDeviceGetPowerManagementLimit"):
            self._nvml.nvmlDeviceGetPowerManagementLimit.argtypes = [
                c_void_p,
                ctypes.POINTER(c_uint),
            ]
            self._nvml.nvmlDeviceGetPowerManagementLimit.restype = c_int
        if hasattr(self._nvml, "nvmlDeviceGetPowerManagementDefaultLimit"):
            self._nvml.nvmlDeviceGetPowerManagementDefaultLimit.argtypes = [
                c_void_p,
                ctypes.POINTER(c_uint),
            ]
            self._nvml.nvmlDeviceGetPowerManagementDefaultLimit.restype = c_int
        if hasattr(self._nvml, "nvmlDeviceGetPowerManagementLimitConstraints"):
            self._nvml.nvmlDeviceGetPowerManagementLimitConstraints.argtypes = [
                c_void_p,
                ctypes.POINTER(c_uint),
                ctypes.POINTER(c_uint),
            ]
            self._nvml.nvmlDeviceGetPowerManagementLimitConstraints.restype = c_int
        if hasattr(self._nvml, "nvmlDeviceSetPowerManagementLimit"):
            self._nvml.nvmlDeviceSetPowerManagementLimit.argtypes = [c_void_p, c_uint]
            self._nvml.nvmlDeviceSetPowerManagementLimit.restype = c_int
        if hasattr(self._nvml, "nvmlDeviceSetPersistenceMode"):
            self._nvml.nvmlDeviceSetPersistenceMode.argtypes = [c_void_p, c_uint]
            self._nvml.nvmlDeviceSetPersistenceMode.restype = c_int
        if hasattr(self._nvml, "nvmlDeviceSetGpuLockedClocks"):
            self._nvml.nvmlDeviceSetGpuLockedClocks.argtypes = [
                c_void_p,
                c_uint,
                c_uint,
            ]
            self._nvml.nvmlDeviceSetGpuLockedClocks.restype = c_int
        if hasattr(self._nvml, "nvmlDeviceResetGpuLockedClocks"):
            self._nvml.nvmlDeviceResetGpuLockedClocks.argtypes = [c_void_p]
            self._nvml.nvmlDeviceResetGpuLockedClocks.restype = c_int
        if hasattr(self._nvml, "nvmlDeviceResetMemoryLockedClocks"):
            self._nvml.nvmlDeviceResetMemoryLockedClocks.argtypes = [c_void_p]
            self._nvml.nvmlDeviceResetMemoryLockedClocks.restype = c_int
        if hasattr(self._nvml, "nvmlDeviceGetSupportedMemoryClocks"):
            self._nvml.nvmlDeviceGetSupportedMemoryClocks.argtypes = [
                c_void_p,
                ctypes.POINTER(c_uint),
                ctypes.POINTER(c_uint),
            ]
            self._nvml.nvmlDeviceGetSupportedMemoryClocks.restype = c_int
        if hasattr(self._nvml, "nvmlDeviceGetSupportedGraphicsClocks"):
            self._nvml.nvmlDeviceGetSupportedGraphicsClocks.argtypes = [
                c_void_p,
                c_uint,
                ctypes.POINTER(c_uint),
                ctypes.POINTER(c_uint),
            ]
            self._nvml.nvmlDeviceGetSupportedGraphicsClocks.restype = c_int
        if hasattr(self._nvml, "nvmlDeviceGetMemClkMinMaxVfOffset"):
            self._nvml.nvmlDeviceGetMemClkMinMaxVfOffset.argtypes = [
                c_void_p,
                ctypes.POINTER(c_int),
                ctypes.POINTER(c_int),
            ]
            self._nvml.nvmlDeviceGetMemClkMinMaxVfOffset.restype = c_int

        if hasattr(self._nvml, "nvmlDeviceGetGpcClkVfOffset"):
            self._nvml.nvmlDeviceGetGpcClkVfOffset.argtypes = [
                c_void_p,
                ctypes.POINTER(c_int),
            ]
            self._nvml.nvmlDeviceGetGpcClkVfOffset.restype = c_int
        if hasattr(self._nvml, "nvmlDeviceSetGpcClkVfOffset"):
            self._nvml.nvmlDeviceSetGpcClkVfOffset.argtypes = [c_void_p, c_int]
            self._nvml.nvmlDeviceSetGpcClkVfOffset.restype = c_int
        if hasattr(self._nvml, "nvmlDeviceGetMemClkVfOffset"):
            self._nvml.nvmlDeviceGetMemClkVfOffset.argtypes = [
                c_void_p,
                ctypes.POINTER(c_int),
            ]
            self._nvml.nvmlDeviceGetMemClkVfOffset.restype = c_int
        if hasattr(self._nvml, "nvmlDeviceSetMemClkVfOffset"):
            self._nvml.nvmlDeviceSetMemClkVfOffset.argtypes = [c_void_p, c_int]
            self._nvml.nvmlDeviceSetMemClkVfOffset.restype = c_int

    def _initialize_session(self):
        rc = int(self._nvml.nvmlInit_v2())
        if rc != NVML_SUCCESS:
            raise RuntimeError(
                f"nvmlInit_v2 failed with NVML error {rc}: {self.error_text(rc)}"
            )
        self._initialized = True

        rc = int(
            self._nvml.nvmlDeviceGetHandleByIndex_v2(
                ctypes.c_uint(self._gpu_index),
                ctypes.byref(self._device),
            )
        )
        if rc != NVML_SUCCESS:
            self.close()
            raise RuntimeError(
                f"nvmlDeviceGetHandleByIndex_v2 failed with NVML error {rc}: {self.error_text(rc)}"
            )

    def error_text(self, rc):
        if hasattr(self._nvml, "nvmlErrorString"):
            text = self._nvml.nvmlErrorString(int(rc))
            if text:
                return text.decode(errors="replace")
        return f"error={rc}"

    def close(self):
        if self._initialized:
            self._nvml.nvmlShutdown()
            self._initialized = False

    def _read_power_value_w(self, getter_name):
        getter = getattr(self._nvml, getter_name, None)
        if getter is None:
            return None

        out = ctypes.c_uint()
        rc = int(getter(self._device, ctypes.byref(out)))
        if rc != NVML_SUCCESS:
            return None
        return int(round(out.value / 1000.0))

    def query_gpu_name(self):
        getter = getattr(self._nvml, "nvmlDeviceGetName", None)
        if getter is None:
            return None

        buf = ctypes.create_string_buffer(96)
        rc = int(getter(self._device, buf, ctypes.c_uint(len(buf))))
        if rc != NVML_SUCCESS:
            return None
        value = buf.value.decode(errors="replace").strip()
        return value or None

    def query_pci_device_id(self):
        getter = getattr(self._nvml, "nvmlDeviceGetPciInfo_v3", None) or getattr(
            self._nvml,
            "nvmlDeviceGetPciInfo_v2",
            None,
        )
        if getter is None:
            return None
        info = NvmlPciInfo()
        rc = int(getter(self._device, ctypes.byref(info)))
        if rc != NVML_SUCCESS or int(info.pciDeviceId) == 0:
            return None
        return f"0x{int(info.pciDeviceId):08X}"

    def query_power_limits(self):
        # These getters can legitimately be unsupported on laptops. Treat
        # failures as absent read-only telemetry; setter support is probed
        # separately and is not implied by min/max-looking values.
        info = {
            "power_limit_w": self._read_power_value_w(
                "nvmlDeviceGetPowerManagementLimit"
            ),
            "power_limit_default_w": self._read_power_value_w(
                "nvmlDeviceGetPowerManagementDefaultLimit"
            ),
            "power_limit_min_w": None,
            "power_limit_max_w": None,
        }

        getter = getattr(
            self._nvml, "nvmlDeviceGetPowerManagementLimitConstraints", None
        )
        if getter is None:
            return info

        min_limit_mw = ctypes.c_uint()
        max_limit_mw = ctypes.c_uint()
        rc = int(
            getter(
                self._device,
                ctypes.byref(min_limit_mw),
                ctypes.byref(max_limit_mw),
            )
        )
        if rc == NVML_SUCCESS:
            info["power_limit_min_w"] = int(round(min_limit_mw.value / 1000.0))
            info["power_limit_max_w"] = int(round(max_limit_mw.value / 1000.0))
        return info

    def apply_power_limit_w(self, power_limit_w):
        setter = getattr(self._nvml, "nvmlDeviceSetPowerManagementLimit", None)
        if setter is None:
            raise RuntimeError(
                "nvmlDeviceSetPowerManagementLimit is not available on this system"
            )

        target_mw = int(round(float(power_limit_w) * 1000.0))
        rc = int(setter(self._device, ctypes.c_uint(target_mw)))
        if rc != NVML_SUCCESS:
            raise RuntimeError(
                "nvmlDeviceSetPowerManagementLimit failed with "
                f"NVML error {rc}: {self.error_text(rc)}"
            )
        return int(power_limit_w)

    def enable_persistence_mode(self):
        setter = getattr(self._nvml, "nvmlDeviceSetPersistenceMode", None)
        if setter is None:
            raise RuntimeError(
                "nvmlDeviceSetPersistenceMode is not available on this system"
            )

        rc = int(setter(self._device, ctypes.c_uint(NVML_FEATURE_ENABLED)))
        if rc != NVML_SUCCESS:
            raise RuntimeError(
                "nvmlDeviceSetPersistenceMode failed with "
                f"NVML error {rc}: {self.error_text(rc)}"
            )
        return True

    def _read_clock_list(self, getter_name, *getter_args, capacity=512):
        getter = getattr(self._nvml, getter_name, None)
        if getter is None:
            return []

        count = ctypes.c_uint(int(capacity))
        values = (ctypes.c_uint * int(capacity))()
        rc = int(getter(self._device, *getter_args, ctypes.byref(count), values))
        if rc != NVML_SUCCESS:
            return []
        return [int(values[index]) for index in range(int(count.value))]

    def get_supported_memory_clocks_mhz(self):
        return self._read_clock_list("nvmlDeviceGetSupportedMemoryClocks", capacity=64)

    def get_supported_core_clocks_mhz(self, memory_clock_mhz):
        return self._read_clock_list(
            "nvmlDeviceGetSupportedGraphicsClocks",
            ctypes.c_uint(int(memory_clock_mhz)),
            capacity=512,
        )

    def get_supported_core_clock_steps_mhz(self):
        memory_clocks = self.get_supported_memory_clocks_mhz()
        core_clocks = set()
        for memory_clock_mhz in memory_clocks:
            core_clocks.update(self.get_supported_core_clocks_mhz(memory_clock_mhz))
        return sorted(core_clocks)

    def snap_core_clock_mhz(self, target_clock_mhz, *, prefer_not_above=True):
        supported_steps = self.get_supported_core_clock_steps_mhz()
        if not supported_steps:
            return {
                "requested_clock_mhz": int(target_clock_mhz),
                "applied_clock_mhz": int(target_clock_mhz),
                "mode": "unsupported-list-unavailable",
                "supported_steps_mhz": [],
            }

        requested_clock_mhz = int(target_clock_mhz)
        if requested_clock_mhz in supported_steps:
            applied_clock_mhz = requested_clock_mhz
            mode = "exact"
        else:
            lower_steps = [
                clock_mhz
                for clock_mhz in supported_steps
                if clock_mhz <= requested_clock_mhz
            ]
            upper_steps = [
                clock_mhz
                for clock_mhz in supported_steps
                if clock_mhz >= requested_clock_mhz
            ]
            if prefer_not_above and lower_steps:
                applied_clock_mhz = max(lower_steps)
                mode = "floor"
            elif upper_steps:
                applied_clock_mhz = min(upper_steps)
                mode = "ceil"
            elif lower_steps:
                applied_clock_mhz = max(lower_steps)
                mode = "floor"
            else:
                applied_clock_mhz = min(
                    supported_steps, key=lambda value: abs(value - requested_clock_mhz)
                )
                mode = "nearest"

        return {
            "requested_clock_mhz": requested_clock_mhz,
            "applied_clock_mhz": int(applied_clock_mhz),
            "mode": mode,
            "supported_steps_mhz": supported_steps,
        }

    def apply_locked_core_clock_mhz(
        self,
        clock_mhz,
        *,
        prefer_not_above=True,
        snap_to_supported=True,
    ):
        setter = getattr(self._nvml, "nvmlDeviceSetGpuLockedClocks", None)
        if setter is None:
            raise RuntimeError(
                "nvmlDeviceSetGpuLockedClocks is not available on this system"
            )

        snap = {
            "requested_clock_mhz": int(clock_mhz),
            "applied_clock_mhz": int(clock_mhz),
            "mode": "unsnapped",
            "supported_steps_mhz": [],
        }
        if snap_to_supported:
            snap = self.snap_core_clock_mhz(
                clock_mhz, prefer_not_above=prefer_not_above
            )

        applied_clock_mhz = int(snap["applied_clock_mhz"])
        rc = int(
            setter(
                self._device,
                ctypes.c_uint(applied_clock_mhz),
                ctypes.c_uint(applied_clock_mhz),
            )
        )
        if rc != NVML_SUCCESS:
            raise RuntimeError(
                "nvmlDeviceSetGpuLockedClocks failed with "
                f"NVML error {rc}: {self.error_text(rc)}"
            )
        return snap

    def apply_locked_core_clock_range_mhz(
        self,
        min_clock_mhz,
        max_clock_mhz,
        *,
        prefer_max_not_above=True,
        snap_to_supported=True,
    ):
        setter = getattr(self._nvml, "nvmlDeviceSetGpuLockedClocks", None)
        if setter is None:
            raise RuntimeError(
                "nvmlDeviceSetGpuLockedClocks is not available on this system"
            )

        range_snap = {
            "requested_min_clock_mhz": int(min_clock_mhz),
            "requested_max_clock_mhz": int(max_clock_mhz),
            "applied_min_clock_mhz": int(min_clock_mhz),
            "applied_max_clock_mhz": int(max_clock_mhz),
            "min_mode": "unsnapped",
            "max_mode": "unsnapped",
            "supported_steps_mhz": [],
        }
        if snap_to_supported:
            min_snap = self.snap_core_clock_mhz(min_clock_mhz, prefer_not_above=False)
            max_snap = self.snap_core_clock_mhz(
                max_clock_mhz, prefer_not_above=prefer_max_not_above
            )
            applied_min_clock_mhz = int(min_snap["applied_clock_mhz"])
            applied_max_clock_mhz = int(max_snap["applied_clock_mhz"])
            min_mode = str(min_snap["mode"])
            max_mode = str(max_snap["mode"])
            supported_steps_mhz = list(
                max_snap["supported_steps_mhz"] or min_snap["supported_steps_mhz"]
            )
            if applied_min_clock_mhz > applied_max_clock_mhz:
                applied_min_clock_mhz = applied_max_clock_mhz
                min_mode = f"{min_mode}-clamped-to-max"
            range_snap = {
                "requested_min_clock_mhz": int(min_clock_mhz),
                "requested_max_clock_mhz": int(max_clock_mhz),
                "applied_min_clock_mhz": applied_min_clock_mhz,
                "applied_max_clock_mhz": applied_max_clock_mhz,
                "min_mode": min_mode,
                "max_mode": max_mode,
                "supported_steps_mhz": supported_steps_mhz,
            }

        rc = int(
            setter(
                self._device,
                ctypes.c_uint(int(range_snap["applied_min_clock_mhz"])),
                ctypes.c_uint(int(range_snap["applied_max_clock_mhz"])),
            )
        )
        if rc != NVML_SUCCESS:
            raise RuntimeError(
                "nvmlDeviceSetGpuLockedClocks failed with "
                f"NVML error {rc}: {self.error_text(rc)}"
            )
        return range_snap

    def reset_locked_core_clocks(self):
        setter = getattr(self._nvml, "nvmlDeviceResetGpuLockedClocks", None)
        if setter is None:
            raise RuntimeError(
                "nvmlDeviceResetGpuLockedClocks is not available on this system"
            )

        rc = int(setter(self._device))
        if rc != NVML_SUCCESS:
            raise RuntimeError(
                "nvmlDeviceResetGpuLockedClocks failed with "
                f"NVML error {rc}: {self.error_text(rc)}"
            )
        return True

    def reset_locked_memory_clocks(self):
        setter = getattr(self._nvml, "nvmlDeviceResetMemoryLockedClocks", None)
        if setter is None:
            raise RuntimeError(
                "nvmlDeviceResetMemoryLockedClocks is not available on this system"
            )

        rc = int(setter(self._device))
        if rc != NVML_SUCCESS:
            raise RuntimeError(
                "nvmlDeviceResetMemoryLockedClocks failed with "
                f"NVML error {rc}: {self.error_text(rc)}"
            )
        return True

    def _read_clock_offset(self, getter_name):
        getter = getattr(self._nvml, getter_name, None)
        if getter is None:
            return None

        out = ctypes.c_int()
        rc = int(getter(self._device, ctypes.byref(out)))
        if rc != NVML_SUCCESS:
            return None
        return int(out.value)

    def get_clock_offsets(self):
        return {
            "gpc_clk_vf_offset_mhz": self._read_clock_offset(
                "nvmlDeviceGetGpcClkVfOffset"
            ),
            "mem_clk_vf_offset_mhz": self._read_clock_offset(
                "nvmlDeviceGetMemClkVfOffset"
            ),
        }

    def get_memory_clock_offset_range_mhz(self):
        getter = getattr(self._nvml, "nvmlDeviceGetMemClkMinMaxVfOffset", None)
        if getter is None:
            return None
        min_value = ctypes.c_int()
        max_value = ctypes.c_int()
        rc = int(getter(self._device, ctypes.byref(min_value), ctypes.byref(max_value)))
        if rc != NVML_SUCCESS:
            return None
        return int(min_value.value), int(max_value.value)

    def apply_clock_offsets(
        self, *, gpc_clk_vf_offset_mhz=None, mem_clk_vf_offset_mhz=None
    ):
        applied = {}

        if gpc_clk_vf_offset_mhz is not None:
            setter = getattr(self._nvml, "nvmlDeviceSetGpcClkVfOffset", None)
            if setter is None:
                raise RuntimeError(
                    "nvmlDeviceSetGpcClkVfOffset is not available on this system"
                )
            rc = int(setter(self._device, ctypes.c_int(int(gpc_clk_vf_offset_mhz))))
            if rc != NVML_SUCCESS:
                raise RuntimeError(
                    f"nvmlDeviceSetGpcClkVfOffset failed with NVML error {rc}: {self.error_text(rc)}"
                )
            applied["gpc_clk_vf_offset_mhz"] = int(gpc_clk_vf_offset_mhz)
            applied["gpc_clk_vf_offset_readback_mhz"] = self._read_clock_offset(
                "nvmlDeviceGetGpcClkVfOffset"
            )

        if mem_clk_vf_offset_mhz is not None:
            setter = getattr(self._nvml, "nvmlDeviceSetMemClkVfOffset", None)
            if setter is None:
                raise RuntimeError(
                    "nvmlDeviceSetMemClkVfOffset is not available on this system"
                )
            rc = int(setter(self._device, ctypes.c_int(int(mem_clk_vf_offset_mhz))))
            if rc != NVML_SUCCESS:
                raise RuntimeError(
                    f"nvmlDeviceSetMemClkVfOffset failed with NVML error {rc}: {self.error_text(rc)}"
                )
            applied["mem_clk_vf_offset_mhz"] = int(mem_clk_vf_offset_mhz)
            # NVML_SUCCESS does not guarantee the offset stuck (issue #20):
            # read it back so callers can log requested-vs-actual.
            applied["mem_clk_vf_offset_readback_mhz"] = self._read_clock_offset(
                "nvmlDeviceGetMemClkVfOffset"
            )

        return applied
