import auto_uv.initial_check.auto_uv_hardware_initial_check as initial_check
from drivers.nvidia.daemon_gpu import VfPoint


def _good_points(count: int = 16) -> list[VfPoint]:
    return [
        {
            "index": index,
            "type": 0,
            "voltage_based": 1,
            "freq_khz": 1_700_000 + index * 15_000,
            "voltage_uv": 700_000 + index * 25_000,
            "base_freq_khz": 1_650_000 + index * 15_000,
            "base_voltage_uv": 700_000 + index * 25_000,
            "current_offset_khz": index * 1_000,
        }
        for index in range(count)
    ]


def _bad_zero_points(count: int = 16) -> list[VfPoint]:
    return [
        {
            "index": index,
            "type": 0,
            "voltage_based": 1,
            "freq_khz": 0,
            "voltage_uv": 450_000 if index % 2 == 0 else 0,
            "base_freq_khz": 0,
            "base_voltage_uv": 450_000 if index % 2 == 0 else 0,
            "current_offset_khz": 0,
        }
        for index in range(count)
    ]


class FakeGpuClient:
    def __init__(
        self,
        points=None,
        *,
        setter_error=None,
        steps=None,
        lock_error=None,
        voltage_uv=900_000,
        raw_voltage_uv=900_000,
    ):
        self._points = points if points is not None else _good_points()
        self._setter_error = setter_error
        self._steps = list(steps if steps is not None else [300, 600, 1_200, 2_400])
        self._lock_error = lock_error
        self._voltage_uv = voltage_uv
        self._raw_voltage_uv = raw_voltage_uv
        self.applied_offsets = None

    def editable_core_points(self) -> list[VfPoint]:
        return list(self._points)

    def apply_offsets_khz(self, offsets):
        if self._setter_error is not None:
            raise RuntimeError(self._setter_error)
        self.applied_offsets = list(offsets)

    def get_supported_core_clock_steps_mhz(self):
        return list(self._steps)

    def apply_locked_core_clock_range_mhz(
        self,
        min_clock_mhz,
        max_clock_mhz,
        *,
        snap_to_supported=True,
    ):
        if self._lock_error is not None:
            raise RuntimeError(self._lock_error)
        return {
            "applied_min_clock_mhz": int(min_clock_mhz),
            "applied_max_clock_mhz": int(max_clock_mhz),
        }

    def reset_locked_core_clocks(self):
        return True

    def read_microvolts(self):
        return self._voltage_uv

    def last_raw_microvolts(self):
        return self._raw_voltage_uv


def _patch_probe_dependencies(
    monkeypatch,
    *,
    voltage_issues=None,
    architecture=10,
    name="NVIDIA GeForce RTX 5080",
    driver_version="595.58.03",
    pci_device_id="0x2C0210DE",
):
    monkeypatch.setattr(
        initial_check,
        "_query_nvml_gpus",
        lambda: [
            initial_check.InitialCheckGpuInfo(
                index=0,
                name=name,
                driver_version=driver_version,
                pci_device_id=pci_device_id,
                uuid="GPU-test",
                architecture=architecture,
            )
        ],
    )
    monkeypatch.setattr(
        initial_check,
        "_validate_nvapi_voltage_reader",
        lambda _client: list(voltage_issues or []),
    )


def _issue_ids(result):
    return {issue.check_id for issue in result.issues}


def test_initial_check_passes_when_driver_and_capability_probes_are_sane(monkeypatch):
    _patch_probe_dependencies(monkeypatch)

    result = initial_check.run_auto_uv_initial_check(
        gpu_client_factory=lambda _gpu_index: FakeGpuClient(),
    )

    assert result.ok
    assert result.gpu is not None
    assert result.gpu.name == "NVIDIA GeForce RTX 5080"
    assert result.gpu.driver_version == "595.58.03"


def test_initial_check_rejects_old_driver_even_when_capability_probes_pass(monkeypatch):
    _patch_probe_dependencies(monkeypatch, driver_version="575.64.03")

    def fail_if_called(gpu_index):
        raise AssertionError("driver check should stop hidden voltage/VF probes")

    monkeypatch.setattr(
        initial_check,
        "_validate_nvapi_voltage_reader",
        lambda client: fail_if_called(client),
    )

    result = initial_check.run_auto_uv_initial_check(
        gpu_client_factory=fail_if_called,
    )

    assert "driver-version" in _issue_ids(result)
    assert "unsupported-gpu" not in _issue_ids(result)
    assert "580.xx or newer" in result.format_for_user()


def test_initial_check_rejects_old_gpu_by_failed_probes_not_by_name(monkeypatch):
    _patch_probe_dependencies(
        monkeypatch,
        architecture=6,
        name="NVIDIA GeForce GTX 1050 Mobile",
        pci_device_id="0x1C8D10DE",
    )

    result = initial_check.run_auto_uv_initial_check(
        gpu_client_factory=lambda _gpu_index: FakeGpuClient(
            _bad_zero_points(),
            lock_error="nvmlDeviceSetGpuLockedClocks failed with NVML error 3: Not Supported",
        ),
    )

    ids = _issue_ids(result)
    assert "driver-version" not in ids
    assert "nvapi-vf-invalid-points" in ids
    assert "nvapi-vf-implausible" in ids
    assert "nvml-clock-lock" in ids
    assert "unsupported-gpu" not in ids

    message = result.format_for_user()
    assert "RTX 50-series Blackwell" in message
    assert "RTX 40-series Ada Lovelace" in message
    assert "RTX 30-series Ampere" in message
    assert initial_check.AUTO_UV_SUPPORT_ISSUE_URL in message


def test_initial_check_rejects_nvapi_voltage_reader_failure(monkeypatch):
    _patch_probe_dependencies(
        monkeypatch,
        voltage_issues=[
            initial_check.InitialCheckIssue(
                "error",
                "nvapi-voltage-reader",
                "NVAPI voltage reader returned no sane voltage",
                "The live voltage sample was 0mV.",
            )
        ],
    )

    result = initial_check.run_auto_uv_initial_check(
        gpu_client_factory=lambda _gpu_index: FakeGpuClient(),
    )

    assert "nvapi-voltage-reader" in _issue_ids(result)
    assert "0mV" in result.format_for_user()


def test_nvapi_voltage_idle_implausible_sample_is_warning(monkeypatch):
    class FakeVoltageReader:
        def read_microvolts(self):
            return None

        def last_raw_microvolts(self):
            return 0

    reader = FakeVoltageReader()
    issues = initial_check._validate_nvapi_voltage_reader(reader)

    assert len(issues) == 1
    assert issues[0].severity == "warning"
    assert "idle voltage" in issues[0].title
    assert "0mV" in issues[0].detail


def test_initial_check_rejects_nvapi_setter_failure(monkeypatch):
    _patch_probe_dependencies(monkeypatch)

    result = initial_check.run_auto_uv_initial_check(
        gpu_client_factory=lambda _gpu_index: FakeGpuClient(
            setter_error="ClockClientClkVfPointsSetControl failed"
        ),
    )

    assert "nvapi-vf-setter" in _issue_ids(result)
    assert "no-op failed" in result.format_for_user()


def test_initial_check_rejects_nvapi_reader_factory_exception(monkeypatch):
    _patch_probe_dependencies(monkeypatch)

    def failing_reader(gpu_index):
        raise RuntimeError("nvapi_QueryInterface returned NULL")

    result = initial_check.run_auto_uv_initial_check(
        gpu_client_factory=failing_reader,
    )

    assert "nvapi-vf-reader" in _issue_ids(result)
    assert "nvapi_QueryInterface returned NULL" in result.format_for_user()


def test_initial_check_rejects_too_few_vf_points(monkeypatch):
    _patch_probe_dependencies(monkeypatch)

    result = initial_check.run_auto_uv_initial_check(
        gpu_client_factory=lambda _gpu_index: FakeGpuClient(_good_points(4)),
    )

    assert "nvapi-vf-point-count" in _issue_ids(result)


def test_driver_version_accepts_major_only_580():
    assert initial_check._validate_driver("580") == []
    assert initial_check._validate_driver("580.01") == []
    assert initial_check._validate_driver("579.99")


def test_ampere_architecture_is_named_for_rtx_30_series() -> None:
    gpu = initial_check.InitialCheckGpuInfo(
        index=0,
        name="NVIDIA GeForce RTX 3080",
        driver_version="595.58.03",
        architecture=initial_check.NVML_DEVICE_ARCH_AMPERE,
    )

    assert gpu.architecture_name == "Ampere"
