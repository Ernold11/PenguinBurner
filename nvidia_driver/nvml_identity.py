"""Public NVML GPU identity helpers."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass


NVML_SUCCESS = 0
NVML_DEVICE_PCI_BUS_ID_BUFFER_SIZE = 32
NVML_DEVICE_PCI_BUS_ID_BUFFER_V2_SIZE = 16
NVML_DEVICE_NAME_BUFFER_SIZE = 96
NVML_DEVICE_UUID_BUFFER_SIZE = 96
NVML_SYSTEM_DRIVER_VERSION_BUFFER_SIZE = 80


class NvmlPciInfo(ctypes.Structure):
    _fields_ = [
        ("busIdLegacy", ctypes.c_char * NVML_DEVICE_PCI_BUS_ID_BUFFER_V2_SIZE),
        ("domain", ctypes.c_uint),
        ("bus", ctypes.c_uint),
        ("device", ctypes.c_uint),
        ("pciDeviceId", ctypes.c_uint),
        ("pciSubSystemId", ctypes.c_uint),
        ("busId", ctypes.c_char * NVML_DEVICE_PCI_BUS_ID_BUFFER_SIZE),
    ]


@dataclass(frozen=True, slots=True)
class NvmlGpuIdentity:
    index: int
    name: str = ""
    driver_version: str = ""
    pci_bus_id: str = ""
    pci_device_id: str = ""
    uuid: str = ""


class NvmlIdentitySession:
    def __init__(self, *, nvml_library=None):
        self._nvml = (
            nvml_library
            if nvml_library is not None
            else ctypes.CDLL("libnvidia-ml.so.1")
        )
        self._initialized = False
        self._bind_functions()
        self._initialize_session()

    def _bind_functions(self) -> None:
        c_uint = ctypes.c_uint
        c_void_p = ctypes.c_void_p
        c_char_p = ctypes.c_char_p

        self._nvml.nvmlInit_v2.restype = ctypes.c_int
        self._nvml.nvmlShutdown.restype = ctypes.c_int
        self._nvml.nvmlDeviceGetHandleByIndex_v2.argtypes = [
            c_uint,
            ctypes.POINTER(c_void_p),
        ]
        self._nvml.nvmlDeviceGetHandleByIndex_v2.restype = ctypes.c_int

        for name in ("nvmlDeviceGetCount_v2", "nvmlDeviceGetCount"):
            getter = getattr(self._nvml, name, None)
            if getter is not None:
                getter.argtypes = [ctypes.POINTER(c_uint)]
                getter.restype = ctypes.c_int

        if hasattr(self._nvml, "nvmlSystemGetDriverVersion"):
            self._nvml.nvmlSystemGetDriverVersion.argtypes = [c_char_p, c_uint]
            self._nvml.nvmlSystemGetDriverVersion.restype = ctypes.c_int
        if hasattr(self._nvml, "nvmlDeviceGetName"):
            self._nvml.nvmlDeviceGetName.argtypes = [c_void_p, c_char_p, c_uint]
            self._nvml.nvmlDeviceGetName.restype = ctypes.c_int
        if hasattr(self._nvml, "nvmlDeviceGetUUID"):
            self._nvml.nvmlDeviceGetUUID.argtypes = [c_void_p, c_char_p, c_uint]
            self._nvml.nvmlDeviceGetUUID.restype = ctypes.c_int
        for name in ("nvmlDeviceGetPciInfo_v3", "nvmlDeviceGetPciInfo_v2"):
            getter = getattr(self._nvml, name, None)
            if getter is not None:
                getter.argtypes = [c_void_p, ctypes.POINTER(NvmlPciInfo)]
                getter.restype = ctypes.c_int

    def _initialize_session(self) -> None:
        rc = int(self._nvml.nvmlInit_v2())
        if rc != NVML_SUCCESS:
            raise RuntimeError(f"nvmlInit_v2 failed with NVML error {rc}")
        self._initialized = True

    def close(self) -> None:
        if self._initialized:
            self._nvml.nvmlShutdown()
            self._initialized = False

    def gpu_count(self) -> int:
        getter = getattr(self._nvml, "nvmlDeviceGetCount_v2", None) or getattr(
            self._nvml, "nvmlDeviceGetCount", None
        )
        if getter is None:
            raise RuntimeError("NVML device count query is not available")
        count = ctypes.c_uint()
        rc = int(getter(ctypes.byref(count)))
        if rc != NVML_SUCCESS:
            raise RuntimeError(f"NVML device count query failed with error {rc}")
        return int(count.value)

    def identity(self, gpu_index: int) -> NvmlGpuIdentity:
        index = int(gpu_index)
        device = self._device_handle(index)
        return NvmlGpuIdentity(
            index=index,
            name=self._read_string(device, "nvmlDeviceGetName", NVML_DEVICE_NAME_BUFFER_SIZE),
            driver_version=self.driver_version(),
            pci_bus_id=self._pci_bus_id(device),
            pci_device_id=self._pci_device_id(device),
            uuid=self._read_string(device, "nvmlDeviceGetUUID", NVML_DEVICE_UUID_BUFFER_SIZE),
        )

    def identities(self) -> list[NvmlGpuIdentity]:
        return [self.identity(index) for index in range(self.gpu_count())]

    def driver_version(self) -> str:
        getter = getattr(self._nvml, "nvmlSystemGetDriverVersion", None)
        if getter is None:
            return ""
        buf = ctypes.create_string_buffer(NVML_SYSTEM_DRIVER_VERSION_BUFFER_SIZE)
        rc = int(getter(buf, ctypes.c_uint(len(buf))))
        if rc != NVML_SUCCESS:
            return ""
        return _decode_bytes(buf.value)

    def _device_handle(self, gpu_index: int):
        device = ctypes.c_void_p()
        rc = int(
            self._nvml.nvmlDeviceGetHandleByIndex_v2(
                ctypes.c_uint(int(gpu_index)),
                ctypes.byref(device),
            )
        )
        if rc != NVML_SUCCESS:
            raise RuntimeError(
                f"nvmlDeviceGetHandleByIndex_v2 failed with NVML error {rc}"
            )
        return device

    def _read_string(self, device, getter_name: str, size: int) -> str:
        getter = getattr(self._nvml, getter_name, None)
        if getter is None:
            return ""
        buf = ctypes.create_string_buffer(int(size))
        rc = int(getter(device, buf, ctypes.c_uint(len(buf))))
        if rc != NVML_SUCCESS:
            return ""
        return _decode_bytes(buf.value)

    def _pci_info(self, device) -> NvmlPciInfo | None:
        getter = getattr(self._nvml, "nvmlDeviceGetPciInfo_v3", None) or getattr(
            self._nvml, "nvmlDeviceGetPciInfo_v2", None
        )
        if getter is None:
            return None
        info = NvmlPciInfo()
        rc = int(getter(device, ctypes.byref(info)))
        if rc != NVML_SUCCESS:
            return None
        return info

    def _pci_bus_id(self, device) -> str:
        info = self._pci_info(device)
        if info is None:
            return ""
        bus_id = _decode_bytes(info.busId)
        if bus_id:
            return bus_id
        legacy = _decode_bytes(info.busIdLegacy)
        if legacy:
            return legacy
        return f"{int(info.domain):08x}:{int(info.bus):02x}:{int(info.device):02x}.0"

    def _pci_device_id(self, device) -> str:
        info = self._pci_info(device)
        if info is None or int(info.pciDeviceId) == 0:
            return ""
        return f"0x{int(info.pciDeviceId):08X}"


def query_nvml_gpu_identity(gpu_index: int) -> NvmlGpuIdentity | None:
    try:
        session = NvmlIdentitySession()
    except Exception:
        return None
    try:
        return session.identity(int(gpu_index))
    except Exception:
        return None
    finally:
        session.close()


def query_nvml_gpu_identities() -> list[NvmlGpuIdentity]:
    try:
        session = NvmlIdentitySession()
    except Exception:
        return []
    try:
        return session.identities()
    except Exception:
        return []
    finally:
        session.close()


def query_nvml_pci_bus_id(gpu_index: int) -> str:
    identity = query_nvml_gpu_identity(int(gpu_index))
    return "" if identity is None else identity.pci_bus_id


def _decode_bytes(value: bytes) -> str:
    return bytes(value).split(b"\0", 1)[0].decode(errors="replace").strip()
