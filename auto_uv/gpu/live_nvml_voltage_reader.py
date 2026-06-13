"""Read the live GPU voltage through Linux NVAPI for probe validation.

The reader owns the hidden NVAPI voltage session so the rest of Auto-UV can treat voltage reads as a small adapter.
"""

from __future__ import annotations

from hidden_nvapi_voltage import create_hidden_voltage_reader


class LiveNvmlVoltageReader:
    """Compatibility-named adapter for the Auto-UV live voltage reader.

    The original implementation used a hardcoded hidden NVML text offset. The
    class name is kept stable for existing imports, but the implementation now
    uses the undocumented Linux NVAPI voltage query.
    """

    def __init__(self, gpu_index: int):
        self._gpu_index = int(gpu_index)
        self._voltage_reader = create_hidden_voltage_reader(gpu_index=self._gpu_index)

    def read_live_voltage_mv(self) -> int | None:
        if self._voltage_reader is None:
            return None
        voltage_uv = self._voltage_reader.read_microvolts()
        if voltage_uv is None:
            return None
        return int(round(int(voltage_uv) / 1000.0))

    def close(self) -> None:
        if self._voltage_reader is not None:
            self._voltage_reader.close()
            self._voltage_reader = None
