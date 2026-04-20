#!/usr/bin/env python3

from __future__ import annotations

import ctypes
import os


NvAPI_Status = ctypes.c_int32
NvU32 = ctypes.c_uint32
NvS32 = ctypes.c_int32
NvU8 = ctypes.c_uint8
NvPhysicalGpuHandle = ctypes.c_void_p

NVAPI_MAX_PHYSICAL_GPUS = 64
NVAPI_SHORT_STRING_MAX = 64
VF_POINTS_CAPACITY = 255


class ClockClientClkVfPointTupleV1(ctypes.Structure):
    _fields_ = [
        ("freq_khz", NvU32),
        ("voltage_uv", NvU32),
        ("rsvd", NvU8 * 32),
    ]


class ClockClientClkVfPointStatusV3(ctypes.Structure):
    _fields_ = [
        ("type_", NvU32),
        ("freq_khz", NvU32),
        ("voltage_uv", NvU32),
        ("vf_tuple_base", ClockClientClkVfPointTupleV1),
        ("vf_tuple_offset", ClockClientClkVfPointTupleV1),
        ("rsvd", NvU8 * 256),
    ]


class ClockClientClkVfPointsStatusV3(ctypes.Structure):
    _fields_ = [
        ("version", NvU32),
        ("vf_points_mask", NvU32 * 8),
        ("b_vf_tuple_base_supported", NvU8),
        ("rsvd", NvU8 * 64),
        ("vf_points", ClockClientClkVfPointStatusV3 * VF_POINTS_CAPACITY),
    ]


class ClockClientClkVfPointInfoV1(ctypes.Structure):
    _fields_ = [
        ("type_", NvU32),
        ("b_voltage_based", NvU8),
        ("rsvd", NvU8 * 16),
    ]


class ClockClientClkVfPointsInfoV1(ctypes.Structure):
    _fields_ = [
        ("version", NvU32),
        ("vf_points_mask", NvU32 * 8),
        ("rsvd", NvU8 * 32),
        ("vf_points", ClockClientClkVfPointInfoV1 * VF_POINTS_CAPACITY),
    ]


class ClockClientClkVfPointControlProgV1(ctypes.Structure):
    _fields_ = [("freq_offset_khz", NvS32)]


class ClockClientClkVfPointControlDataV1(ctypes.Union):
    _fields_ = [
        ("prog", ClockClientClkVfPointControlProgV1),
        ("rsvd", NvU8 * 16),
    ]


class ClockClientClkVfPointControlV1(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [
        ("type_", NvU32),
        ("rsvd", NvU8 * 16),
        ("data", ClockClientClkVfPointControlDataV1),
    ]


class ClockClientClkVfPointsControlV1(ctypes.Structure):
    _fields_ = [
        ("version", NvU32),
        ("vf_points_mask", NvU32 * 8),
        ("rsvd", NvU8 * 32),
        ("vf_points", ClockClientClkVfPointControlV1 * VF_POINTS_CAPACITY),
    ]


class HiddenNvapiVfCurveReader:
    _QUERY_INITIALIZE = 0x0150E828
    _QUERY_UNLOAD = 0xD22BDD7E
    _QUERY_GET_ERROR_MESSAGE = 0x6C2D048C
    _QUERY_ENUM_PHYSICAL_GPUS = 0xE5AC921F
    _QUERY_GET_VF_INFO = 0x507B4B59
    _QUERY_GET_VF_STATUS = 0x21537AD4
    _QUERY_GET_VF_CONTROL = 0x23F1B133
    _QUERY_SET_VF_CONTROL = 0x0733E009

    def __init__(self, gpu_index=0):
        self._gpu_index = gpu_index
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
        self._get_vf_info = self._query_interface(
            self._QUERY_GET_VF_INFO,
            NvAPI_Status,
            NvPhysicalGpuHandle,
            ctypes.POINTER(ClockClientClkVfPointsInfoV1),
        )
        self._get_vf_status = self._query_interface(
            self._QUERY_GET_VF_STATUS,
            NvAPI_Status,
            NvPhysicalGpuHandle,
            ctypes.POINTER(ClockClientClkVfPointsStatusV3),
        )
        self._get_vf_control = self._query_interface(
            self._QUERY_GET_VF_CONTROL,
            NvAPI_Status,
            NvPhysicalGpuHandle,
            ctypes.POINTER(ClockClientClkVfPointsControlV1),
        )
        self._set_vf_control = self._query_interface(
            self._QUERY_SET_VF_CONTROL,
            NvAPI_Status,
            NvPhysicalGpuHandle,
            ctypes.POINTER(ClockClientClkVfPointsControlV1),
        )
        self._initialized = False
        self._gpu = None
        self._mask = None
        self._initialize_session()
        self._points = self._read_points()

    def _query_interface(self, interface_id, restype, *argtypes):
        query = self._lib.nvapi_QueryInterface
        query.argtypes = [NvU32]
        query.restype = ctypes.c_void_p
        ptr = query(interface_id)
        if not ptr:
            raise RuntimeError(f"nvapi_QueryInterface({interface_id:#x}) returned NULL")
        return ctypes.CFUNCTYPE(restype, *argtypes)(ptr)

    @staticmethod
    def _make_version(struct_type, version):
        return (ctypes.sizeof(struct_type) & 0xFFFF) | (version << 16)

    @staticmethod
    def _mask_to_indices(mask_words):
        indices = []
        for word_index, word in enumerate(mask_words):
            value = int(word)
            for bit in range(32):
                if value & (1 << bit):
                    indices.append(word_index * 32 + bit)
        return indices

    def _initialize_session(self):
        if self._initialized:
            return
        rc = int(self._initialize())
        if rc != 0:
            raise RuntimeError(f"NvAPI_Initialize failed with status {rc}: {self.status_text(rc)}")
        self._initialized = True

        handles = (NvPhysicalGpuHandle * NVAPI_MAX_PHYSICAL_GPUS)()
        count = NvU32(0)
        rc = int(self._enum_physical_gpus(handles, ctypes.byref(count)))
        if rc != 0 or count.value == 0:
            raise RuntimeError(f"NvAPI_EnumPhysicalGPUs failed with status {rc}: {self.status_text(rc)}")

        if self._gpu_index >= count.value:
            raise RuntimeError(f"GPU index {self._gpu_index} is out of range for {count.value} GPU(s)")

        self._gpu = handles[self._gpu_index]

    def _read_points(self):
        if not self._initialized or self._gpu is None:
            raise RuntimeError("NVAPI VF session is not initialized")

        info = self.get_info_struct()
        status = self.get_status_struct(info.vf_points_mask)
        control = self.get_control_struct(info.vf_points_mask)

        self._mask = [int(x) for x in info.vf_points_mask]

        points = []
        for idx in self._mask_to_indices(self._mask):
            point_info = info.vf_points[idx]
            point_status = status.vf_points[idx]
            point_control = control.vf_points[idx]
            points.append(
                {
                    "index": idx,
                    "type": int(point_info.type_),
                    "voltage_based": int(point_info.b_voltage_based),
                    "freq_khz": int(point_status.freq_khz),
                    "voltage_uv": int(point_status.voltage_uv),
                    "base_freq_khz": int(point_status.vf_tuple_base.freq_khz),
                    "base_voltage_uv": int(point_status.vf_tuple_base.voltage_uv),
                    "current_offset_khz": int(point_control.prog.freq_offset_khz),
                }
            )

        return points

    def status_text(self, status):
        buf = ctypes.create_string_buffer(NVAPI_SHORT_STRING_MAX)
        rc = int(self._get_error_message(NvAPI_Status(status), buf))
        if rc == 0:
            return buf.value.decode(errors="replace")
        return f"status={status}"

    def get_info_struct(self):
        info = ClockClientClkVfPointsInfoV1()
        info.version = self._make_version(ClockClientClkVfPointsInfoV1, 1)
        rc = int(self._get_vf_info(self._gpu, ctypes.byref(info)))
        if rc != 0:
            raise RuntimeError(f"ClockClientClkVfPointsGetInfo failed with status {rc}: {self.status_text(rc)}")
        return info

    def get_status_struct(self, mask=None):
        if mask is None:
            mask = self._mask
        status = ClockClientClkVfPointsStatusV3()
        status.version = self._make_version(ClockClientClkVfPointsStatusV3, 3)
        for i, value in enumerate(mask):
            status.vf_points_mask[i] = value
        rc = int(self._get_vf_status(self._gpu, ctypes.byref(status)))
        if rc != 0:
            raise RuntimeError(f"ClockClientClkVfPointsGetStatus failed with status {rc}: {self.status_text(rc)}")
        return status

    def get_control_struct(self, mask=None):
        if mask is None:
            mask = self._mask
        control = ClockClientClkVfPointsControlV1()
        control.version = self._make_version(ClockClientClkVfPointsControlV1, 1)
        for i, value in enumerate(mask):
            control.vf_points_mask[i] = value
        rc = int(self._get_vf_control(self._gpu, ctypes.byref(control)))
        if rc != 0:
            raise RuntimeError(f"ClockClientClkVfPointsGetControl failed with status {rc}: {self.status_text(rc)}")
        return control

    def set_control_struct(self, control):
        rc = int(self._set_vf_control(self._gpu, ctypes.byref(control)))
        if rc != 0:
            raise RuntimeError(f"ClockClientClkVfPointsSetControl failed with status {rc}: {self.status_text(rc)}")
        return rc

    def editable_core_points(self):
        return [
            point
            for point in self._points
            if point["type"] == 0 and point["voltage_based"] == 1
        ]

    def summary(self):
        editable = self.editable_core_points()
        return {
            "active_points": len(self._points),
            "editable_core_points": len(editable),
        }

    def find_nearest_point(self, graphics_clock_mhz, voltage_uv):
        if graphics_clock_mhz is None or voltage_uv is None:
            return None

        points = self.editable_core_points()
        if not points:
            return None

        target_freq_khz = int(graphics_clock_mhz) * 1000
        target_voltage_uv = int(voltage_uv)
        return min(
            points,
            key=lambda point: abs(point["freq_khz"] - target_freq_khz)
            + abs(point["voltage_uv"] - target_voltage_uv),
        )

    def find_point_by_index(self, index):
        for point in self._points:
            if point["index"] == index:
                return point
        return None

    def find_nearest_voltage_point(self, voltage_mv):
        points = self.editable_core_points()
        if not points:
            return None
        target_voltage_uv = int(voltage_mv) * 1000
        return min(points, key=lambda point: abs(point["voltage_uv"] - target_voltage_uv))

    def refresh_points(self):
        self._points = self._read_points()
        return self._points

    def close(self):
        if self._initialized:
            self._unload()
            self._initialized = False


def create_hidden_vf_curve_reader(gpu_index=0):
    try:
        return HiddenNvapiVfCurveReader(gpu_index=gpu_index)
    except Exception:
        return None
