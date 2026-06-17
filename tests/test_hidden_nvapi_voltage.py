from __future__ import annotations

import ctypes

from nvidia_driver import hidden_nvapi_voltage as voltage


class _QueryInterface:
    def __init__(self, mapping: dict[int, int]):
        self._mapping = mapping

    def __call__(self, interface_id):
        return self._mapping.get(int(interface_id), 0)


class _FakeNvApiLibrary:
    def __init__(
        self,
        *,
        voltage_uv: int = 875_000,
        handles: list[int] | None = None,
        bus_ids: dict[int, int] | None = None,
        voltage_uv_by_handle: dict[int, int] | None = None,
        missing_bus_query: bool = False,
        missing_voltage_query: bool = False,
    ):
        self.unloaded = False
        self.handles = list(handles if handles is not None else [0x1234])
        self.bus_ids = dict(bus_ids or {})
        self.voltage_uv_by_handle = dict(voltage_uv_by_handle or {})
        self._callbacks = []

        def initialize():
            return 0

        def unload():
            self.unloaded = True
            return 0

        def enum_gpus(handles, count):
            for index, handle in enumerate(self.handles):
                handles[index] = voltage.NvPhysicalGpuHandle(handle)
            count[0] = len(self.handles)
            return 0

        def error_message(_status, buffer):
            ctypes.memmove(buffer, b"fake nvapi error\0", 17)
            return 0

        def get_bus_id(handle, bus_id):
            bus_id[0] = self.bus_ids.get(_handle_value(handle), 0)
            return 0

        def get_voltage(handle, data):
            data.contents.value_uv = int(
                self.voltage_uv_by_handle.get(_handle_value(handle), voltage_uv)
            )
            return 0

        mapping = {
            voltage.HiddenNvapiVoltageReader._QUERY_INITIALIZE: self._add_callback(
                ctypes.CFUNCTYPE(voltage.NvAPI_Status)(initialize)
            ),
            voltage.HiddenNvapiVoltageReader._QUERY_UNLOAD: self._add_callback(
                ctypes.CFUNCTYPE(voltage.NvAPI_Status)(unload)
            ),
            voltage.HiddenNvapiVoltageReader._QUERY_GET_ERROR_MESSAGE: self._add_callback(
                ctypes.CFUNCTYPE(
                    voltage.NvAPI_Status,
                    voltage.NvAPI_Status,
                    ctypes.c_char_p,
                )(error_message)
            ),
            voltage.HiddenNvapiVoltageReader._QUERY_ENUM_PHYSICAL_GPUS: self._add_callback(
                ctypes.CFUNCTYPE(
                    voltage.NvAPI_Status,
                    ctypes.POINTER(voltage.NvPhysicalGpuHandle),
                    ctypes.POINTER(voltage.NvU32),
                )(enum_gpus)
            ),
        }
        if not missing_bus_query:
            mapping[voltage.HiddenNvapiVoltageReader._QUERY_GET_BUS_ID] = self._add_callback(
                ctypes.CFUNCTYPE(
                    voltage.NvAPI_Status,
                    voltage.NvPhysicalGpuHandle,
                    ctypes.POINTER(voltage.NvU32),
                )(get_bus_id)
            )
        if not missing_voltage_query:
            mapping[voltage.HiddenNvapiVoltageReader._QUERY_VOLTAGE] = self._add_callback(
                ctypes.CFUNCTYPE(
                    voltage.NvAPI_Status,
                    voltage.NvPhysicalGpuHandle,
                    ctypes.POINTER(voltage.NvApiVoltage),
                )(get_voltage)
            )

        self.nvapi_QueryInterface = _QueryInterface(mapping)

    def _add_callback(self, callback) -> int:
        self._callbacks.append(callback)
        return int(ctypes.cast(callback, ctypes.c_void_p).value)


def _handle_value(handle) -> int:
    value = getattr(handle, "value", handle)
    return int(value)


def test_hidden_nvapi_voltage_reader_reads_microvolts(monkeypatch) -> None:
    fake_lib = _FakeNvApiLibrary(voltage_uv=912_000)
    monkeypatch.setattr(voltage.ctypes, "CDLL", lambda _name: fake_lib)
    monkeypatch.setattr(voltage, "query_nvml_pci_bus_id", lambda _gpu_index: "")

    reader = voltage.HiddenNvapiVoltageReader(gpu_index=0)

    assert reader.read_microvolts() == 912_000

    reader.close()
    assert fake_lib.unloaded is True


def test_hidden_nvapi_voltage_reader_reports_missing_query(monkeypatch) -> None:
    fake_lib = _FakeNvApiLibrary(missing_voltage_query=True)
    monkeypatch.setattr(voltage.ctypes, "CDLL", lambda _name: fake_lib)
    monkeypatch.setattr(voltage, "query_nvml_pci_bus_id", lambda _gpu_index: "")

    reader = voltage.create_hidden_voltage_reader(gpu_index=0)

    assert reader is None
    assert "0x465f9bcf" in str(
        voltage.get_hidden_voltage_reader_last_error()
    ).lower()


def test_hidden_nvapi_voltage_reader_matches_nvapi_handle_by_pci_bus(
    monkeypatch,
) -> None:
    fake_lib = _FakeNvApiLibrary(
        handles=[0x1111, 0x2222],
        bus_ids={0x1111: 0x03, 0x2222: 0x2B},
        voltage_uv_by_handle={0x1111: 700_000, 0x2222: 912_000},
    )
    monkeypatch.setattr(voltage.ctypes, "CDLL", lambda _name: fake_lib)
    monkeypatch.setattr(
        voltage,
        "query_nvml_pci_bus_id",
        lambda _gpu_index: "00000000:2B:00.0",
    )

    reader = voltage.HiddenNvapiVoltageReader(gpu_index=0)

    assert reader.read_microvolts() == 912_000


def test_hidden_nvapi_voltage_reader_keeps_raw_implausible_sample(
    monkeypatch,
) -> None:
    fake_lib = _FakeNvApiLibrary(voltage_uv=0)
    monkeypatch.setattr(voltage.ctypes, "CDLL", lambda _name: fake_lib)
    monkeypatch.setattr(voltage, "query_nvml_pci_bus_id", lambda _gpu_index: "")

    reader = voltage.HiddenNvapiVoltageReader(gpu_index=0)

    assert reader.read_microvolts() is None
    assert reader.last_raw_microvolts() == 0
