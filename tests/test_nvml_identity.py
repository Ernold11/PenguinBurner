from __future__ import annotations

from nvidia_driver.nvml_identity import NvmlIdentitySession


class FakeNvmlFunction:
    def __init__(self, impl):
        self.impl = impl

    def __call__(self, *args):
        return self.impl(*args)


class FakeIdentityNvmlLibrary:
    def __init__(self):
        self.shutdown_called = False
        self.nvmlInit_v2 = FakeNvmlFunction(lambda: 0)
        self.nvmlShutdown = FakeNvmlFunction(self._shutdown)
        self.nvmlDeviceGetHandleByIndex_v2 = FakeNvmlFunction(self._handle_by_index)
        self.nvmlDeviceGetMemoryInfo = FakeNvmlFunction(self._memory_info)

    def _shutdown(self):
        self.shutdown_called = True
        return 0

    def _handle_by_index(self, index, device_ptr):
        device_ptr._obj.value = int(index.value) + 100
        return 0

    def _memory_info(self, _device, info_ptr):
        info = info_ptr._obj
        info.total = 8 * 1024**3
        info.free = 3 * 1024**3
        info.used = 5 * 1024**3
        return 0


def test_nvml_identity_session_reads_memory_info() -> None:
    fake_nvml = FakeIdentityNvmlLibrary()
    session = NvmlIdentitySession(nvml_library=fake_nvml)
    try:
        info = session.memory_info(1)
    finally:
        session.close()

    assert info is not None
    assert info.index == 1
    assert info.total_bytes == 8 * 1024**3
    assert info.free_bytes == 3 * 1024**3
    assert info.used_bytes == 5 * 1024**3
    assert fake_nvml.shutdown_called is True
