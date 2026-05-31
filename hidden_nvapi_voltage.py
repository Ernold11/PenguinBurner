#!/usr/bin/env python3

from __future__ import annotations

import ctypes

from hidden_nvapi_gpu_selection import (
    pci_bus_number_from_bus_id,
    query_nvidia_smi_pci_bus_id,
)


NvAPI_Status = ctypes.c_int32
NvU32 = ctypes.c_uint32
NvU8 = ctypes.c_uint8
NvPhysicalGpuHandle = ctypes.c_void_p

NVAPI_MAX_PHYSICAL_GPUS = 64
NVAPI_SHORT_STRING_MAX = 64


class NvApiVoltage(ctypes.Structure):
    _fields_ = [
        ("version", NvU32),
        ("flags", NvU32),
        ("padding_1", NvU32 * 8),
        ("value_uv", NvU32),
        ("padding_2", NvU32 * 8),
    ]


class HiddenNvapiVoltageReader:
    """Best-effort reader for NVIDIA's undocumented Linux NVAPI voltage query."""

    _QUERY_INITIALIZE = 0x0150E828
    _QUERY_UNLOAD = 0xD22BDD7E
    _QUERY_GET_ERROR_MESSAGE = 0x6C2D048C
    _QUERY_ENUM_PHYSICAL_GPUS = 0xE5AC921F
    _QUERY_GET_BUS_ID = 0x1BE0B8E5
    _QUERY_VOLTAGE = 0x465F9BCF

    def __init__(self, gpu_index=0, *, pci_bus_id: str = ""):
        self._gpu_index = int(gpu_index)
        self._requested_pci_bus_id = (
            str(pci_bus_id or "").strip()
            or query_nvidia_smi_pci_bus_id(self._gpu_index)
        )
        self._requested_bus_number = pci_bus_number_from_bus_id(
            self._requested_pci_bus_id
        )
        self._lib = ctypes.CDLL("libnvidia-api.so.1")
        self._initialize = self._query_interface(self._QUERY_INITIALIZE, NvAPI_Status)
        self._unload = self._query_interface(self._QUERY_UNLOAD, NvAPI_Status)
        self._get_error_message = self._query_interface(
            self._QUERY_GET_ERROR_MESSAGE,
            NvAPI_Status,
            NvAPI_Status,
            ctypes.c_char_p,
        )
        self._enum_physical_gpus = self._query_interface(
            self._QUERY_ENUM_PHYSICAL_GPUS,
            NvAPI_Status,
            ctypes.POINTER(NvPhysicalGpuHandle),
            ctypes.POINTER(NvU32),
        )
        self._get_bus_id = self._try_query_interface(
            self._QUERY_GET_BUS_ID,
            NvAPI_Status,
            NvPhysicalGpuHandle,
            ctypes.POINTER(NvU32),
        )
        self._get_voltage = self._query_interface(
            self._QUERY_VOLTAGE,
            NvAPI_Status,
            NvPhysicalGpuHandle,
            ctypes.POINTER(NvApiVoltage),
        )
        self._initialized = False
        self._gpu = None
        self._last_raw_microvolts: int | None = None
        try:
            self._initialize_session()
        except Exception:
            self.close()
            raise

    def _query_interface(self, interface_id, restype, *argtypes):
        query = self._lib.nvapi_QueryInterface
        query.argtypes = [NvU32]
        query.restype = ctypes.c_void_p
        ptr = query(interface_id)
        if not ptr:
            raise RuntimeError(f"nvapi_QueryInterface({interface_id:#x}) returned NULL")
        return ctypes.CFUNCTYPE(restype, *argtypes)(ptr)

    def _try_query_interface(self, interface_id, restype, *argtypes):
        try:
            return self._query_interface(interface_id, restype, *argtypes)
        except RuntimeError:
            return None

    @staticmethod
    def _make_version(struct_type, version):
        return (ctypes.sizeof(struct_type) & 0xFFFF) | (version << 16)

    def _initialize_session(self):
        if self._initialized:
            return
        rc = int(self._initialize())
        if rc != 0:
            raise RuntimeError(
                f"NvAPI_Initialize failed with status {rc}: {self.status_text(rc)}"
            )
        self._initialized = True

        handles = (NvPhysicalGpuHandle * NVAPI_MAX_PHYSICAL_GPUS)()
        count = NvU32(0)
        rc = int(self._enum_physical_gpus(handles, ctypes.byref(count)))
        if rc != 0 or count.value == 0:
            raise RuntimeError(
                f"NvAPI_EnumPhysicalGPUs failed with status {rc}: {self.status_text(rc)}"
            )

        if self._gpu_index >= count.value:
            raise RuntimeError(
                f"GPU index {self._gpu_index} is out of range for {count.value} GPU(s)"
            )

        self._gpu = self._select_gpu_handle(handles, count.value)

    def _select_gpu_handle(self, handles, count: int):
        requested_bus = self._requested_bus_number
        if requested_bus is not None and self._get_bus_id is not None:
            for index in range(int(count)):
                bus_id = NvU32(0)
                rc = int(self._get_bus_id(handles[index], ctypes.byref(bus_id)))
                if rc == 0 and int(bus_id.value) == int(requested_bus):
                    return handles[index]
        return handles[self._gpu_index]

    def status_text(self, status):
        buf = ctypes.create_string_buffer(NVAPI_SHORT_STRING_MAX)
        rc = int(self._get_error_message(NvAPI_Status(status), buf))
        if rc == 0:
            return buf.value.decode(errors="replace")
        return f"status={status}"

    def read_microvolts(self, *_ignored):
        voltage_uv = self.read_raw_microvolts()
        if voltage_uv is None:
            return None
        if voltage_uv < 300_000 or voltage_uv > 1_500_000:
            return None
        return voltage_uv

    def read_raw_microvolts(self):
        if not self._initialized or self._gpu is None:
            return None

        data = NvApiVoltage()
        data.version = self._make_version(NvApiVoltage, 1)
        rc = int(self._get_voltage(self._gpu, ctypes.byref(data)))
        if rc != 0:
            raise RuntimeError(
                f"NvAPI voltage query failed with status {rc}: {self.status_text(rc)}"
            )

        voltage_uv = int(data.value_uv)
        self._last_raw_microvolts = voltage_uv
        return voltage_uv

    def last_raw_microvolts(self) -> int | None:
        return self._last_raw_microvolts

    def close(self):
        if self._initialized:
            self._unload()
            self._initialized = False


_LAST_CREATE_ERROR: Exception | None = None


def get_hidden_voltage_reader_last_error() -> Exception | None:
    return _LAST_CREATE_ERROR


def create_hidden_voltage_reader(gpu_index=0):
    global _LAST_CREATE_ERROR
    try:
        reader = HiddenNvapiVoltageReader(gpu_index=gpu_index)
        _LAST_CREATE_ERROR = None
        return reader
    except Exception as exc:
        _LAST_CREATE_ERROR = exc
        return None
