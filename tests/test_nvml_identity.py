from __future__ import annotations

from drivers.nvidia.nvml_identity import NvmlIdentitySession
from drivers.nvidia.nvml_identity import query_nvml_gpu_identity_result


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
        self.nvmlDeviceGetCount_v2 = FakeNvmlFunction(self._count)
        self.nvmlDeviceGetMemoryInfo = FakeNvmlFunction(self._memory_info)

    def _shutdown(self):
        self.shutdown_called = True
        return 0

    def _count(self, count_ptr):
        count_ptr._obj.value = 1
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


class FailingInitNvmlLibrary(FakeIdentityNvmlLibrary):
    def __init__(self):
        super().__init__()
        self.nvmlInit_v2 = FakeNvmlFunction(lambda: 9)
        self.nvmlErrorString = FakeNvmlFunction(
            lambda rc: b"Driver Not Loaded" if int(rc) == 9 else b"Unknown"
        )


def test_nvml_identity_result_reports_init_failure_detail() -> None:
    result = query_nvml_gpu_identity_result(
        attempts=1,
        delay_s=0,
        nvml_library_factory=FailingInitNvmlLibrary,
    )

    assert result.identities == ()
    assert result.attempts == 1
    assert "nvmlInit_v2 failed with NVML error 9: Driver Not Loaded" in result.error


def test_nvml_identity_result_retries_transient_init_failure() -> None:
    libraries = [FailingInitNvmlLibrary(), FakeIdentityNvmlLibrary()]

    result = query_nvml_gpu_identity_result(
        attempts=2,
        delay_s=0,
        nvml_library_factory=lambda: libraries.pop(0),
    )

    assert result.error == ""
    assert result.attempts == 2
    assert [identity.index for identity in result.identities] == [0]
