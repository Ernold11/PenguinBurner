from __future__ import annotations

import ctypes

import hidden_nvapi_voltage as voltage


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
        missing_voltage_query: bool = False,
    ):
        self.unloaded = False
        self._callbacks = []

        def initialize():
            return 0

        def unload():
            self.unloaded = True
            return 0

        def enum_gpus(handles, count):
            handles[0] = voltage.NvPhysicalGpuHandle(0x1234)
            count[0] = 1
            return 0

        def error_message(_status, buffer):
            ctypes.memmove(buffer, b"fake nvapi error\0", 17)
            return 0

        def get_voltage(_handle, data):
            data.contents.value_uv = int(voltage_uv)
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


def test_hidden_nvapi_voltage_reader_reads_microvolts(monkeypatch) -> None:
    fake_lib = _FakeNvApiLibrary(voltage_uv=912_000)
    monkeypatch.setattr(voltage.ctypes, "CDLL", lambda _name: fake_lib)

    reader = voltage.HiddenNvapiVoltageReader(gpu_index=0)

    assert reader.read_microvolts() == 912_000

    reader.close()
    assert fake_lib.unloaded is True


def test_hidden_nvapi_voltage_reader_reports_missing_query(monkeypatch) -> None:
    fake_lib = _FakeNvApiLibrary(missing_voltage_query=True)
    monkeypatch.setattr(voltage.ctypes, "CDLL", lambda _name: fake_lib)

    reader = voltage.create_hidden_voltage_reader(gpu_index=0)

    assert reader is None
    assert "0x465f9bcf" in str(
        voltage.get_hidden_voltage_reader_last_error()
    ).lower()
