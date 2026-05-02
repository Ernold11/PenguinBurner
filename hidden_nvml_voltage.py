#!/usr/bin/env python3

"""Unused hidden NVML voltage reader kept for reverse-engineering reference.

Auto-UV does not use this module because it depends on driver-version-specific
static text offsets. A stale offset can jump into the wrong code and segfault
the process before Python can report a normal capability error.
"""

from __future__ import annotations

import ctypes


class DlInfo(ctypes.Structure):
    _fields_ = [
        ("dli_fname", ctypes.c_char_p),
        ("dli_fbase", ctypes.c_void_p),
        ("dli_sname", ctypes.c_char_p),
        ("dli_saddr", ctypes.c_void_p),
    ]


class HiddenNvmlVoltageReader:
    """Best-effort reader for NVIDIA's hidden microvolts getter on Linux."""

    _HIDDEN_GET_VOLTAGE_UV_OFFSET = 0x0A68A0
    _DEVICE_OBJECT_OFFSET = 0x256D8

    def __init__(self, nvml):
        self._nvml = nvml
        self._getter = self._resolve_hidden_getter()

    def _resolve_hidden_getter(self):
        libdl = ctypes.CDLL("libdl.so.2")
        dladdr = libdl.dladdr
        dladdr.argtypes = [ctypes.c_void_p, ctypes.POINTER(DlInfo)]
        dladdr.restype = ctypes.c_int

        symbol = ctypes.cast(
            self._nvml.nvmlInternalGetExportTable, ctypes.c_void_p
        ).value
        info = DlInfo()
        rc = dladdr(ctypes.c_void_p(symbol), ctypes.byref(info))
        if rc == 0 or not info.dli_fbase:
            raise RuntimeError("failed to resolve libnvidia-ml base address")

        hidden_addr = int(info.dli_fbase) + self._HIDDEN_GET_VOLTAGE_UV_OFFSET
        return ctypes.CFUNCTYPE(
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        )(hidden_addr)

    def read_microvolts(self, device):
        if not device or not device.value:
            return None

        try:
            obj = ctypes.c_uint64.from_address(
                device.value + self._DEVICE_OBJECT_OFFSET
            ).value
        except (TypeError, ValueError, OSError):
            return None

        if not obj:
            return None

        out = ctypes.c_uint32(0)
        rc = self._getter(ctypes.c_void_p(obj), device, ctypes.byref(out))
        if rc != 0:
            return None

        voltage_uv = int(out.value)
        if voltage_uv < 300_000 or voltage_uv > 1_500_000:
            return None
        return voltage_uv


def create_hidden_voltage_reader(nvml):
    try:
        return HiddenNvmlVoltageReader(nvml)
    except Exception:
        return None
