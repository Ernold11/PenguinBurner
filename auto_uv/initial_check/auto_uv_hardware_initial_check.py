"""Check Nvidia driver and GPU controls before Auto-UV starts.

This package is outside auto_uv because it validates system readiness, not the undervolt algorithm.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable, Protocol, cast

from drivers.nvidia.daemon_gpu import DaemonGpuClient, VfPoint


MINIMUM_NVIDIA_DRIVER_VERSION = (580, 0)
AUTO_UV_SUPPORT_ISSUE_URL = "https://github.com/jpietek/PenguinBurner/issues/3"
NVML_DEVICE_ARCH_AMPERE = 7
NVML_DEVICE_ARCH_ADA = 8
NVML_DEVICE_ARCH_BLACKWELL = 10
NVML_DEVICE_ARCH_NAMES = {
    NVML_DEVICE_ARCH_AMPERE: "Ampere",
    NVML_DEVICE_ARCH_ADA: "Ada Lovelace",
    NVML_DEVICE_ARCH_BLACKWELL: "Blackwell",
}


class _VoltageReader(Protocol):
    def read_microvolts(self) -> int | None: ...

    def last_raw_microvolts(self) -> int | None: ...


class _GpuControl(_VoltageReader, Protocol):
    def editable_core_points(self) -> list[VfPoint]: ...

    def apply_offsets_khz(self, offsets: list[tuple[int, int]]) -> None: ...

    def get_supported_core_clock_steps_mhz(self) -> list[int]: ...

    def apply_locked_core_clock_range_mhz(
        self,
        min_clock_mhz: int,
        max_clock_mhz: int,
        *,
        snap_to_supported: bool = True,
    ) -> object: ...

    def reset_locked_core_clocks(self) -> object: ...

@dataclass(frozen=True, slots=True)
class InitialCheckIssue:
    severity: str
    check_id: str
    title: str
    detail: str
    remediation: str = ""


@dataclass(frozen=True, slots=True)
class InitialCheckGpuInfo:
    index: int
    name: str
    driver_version: str
    pci_device_id: str = ""
    uuid: str = ""
    architecture: int | None = None

    @property
    def architecture_name(self) -> str:
        if self.architecture is None:
            return "unknown"
        return NVML_DEVICE_ARCH_NAMES.get(
            int(self.architecture),
            f"unknown ({int(self.architecture)})",
        )


@dataclass(frozen=True, slots=True)
class InitialCheckResult:
    gpu: InitialCheckGpuInfo | None
    issues: tuple[InitialCheckIssue, ...]

    @property
    def errors(self) -> tuple[InitialCheckIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "error")

    @property
    def warnings(self) -> tuple[InitialCheckIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "warning")

    @property
    def ok(self) -> bool:
        return not self.errors

    def format_for_user(self) -> str:
        lines = ["Auto-UV initial check failed."]
        if self.gpu is not None:
            lines.append(
                "Detected GPU: "
                f"{self.gpu.name} "
                f"(driver {self.gpu.driver_version}, "
                f"architecture {self.gpu.architecture_name})"
            )
        lines.append(
            "Auto-UV requires an up-to-date Nvidia driver, working NVML/NVAPI "
            "voltage and V/F controls, and a supported GPU: GeForce RTX "
            "50-series Blackwell, RTX 40-series Ada Lovelace, or RTX 30-series "
            "Ampere."
        )
        if self.errors:
            lines.append("")
            lines.append("Errors:")
            lines.extend(_format_issue_lines(self.errors))
        if self.warnings:
            lines.append("")
            lines.append("Warnings:")
            lines.extend(_format_issue_lines(self.warnings))
        lines.append("")
        lines.append(f"Related compatibility issue: {AUTO_UV_SUPPORT_ISSUE_URL}")
        return "\n".join(lines)


def run_auto_uv_initial_check(
    *,
    gpu_index: int = 0,
    gpu_client_factory: Callable[[int], _GpuControl] = DaemonGpuClient,
) -> InitialCheckResult:
    issues: list[InitialCheckIssue] = []
    gpu = None
    gpu_rows = _query_nvml_gpus()
    if not gpu_rows:
        issues.append(
            InitialCheckIssue(
                "error",
                "nvidia-gpu-missing",
                "No Nvidia GPU was detected",
                "NVML did not return any physical GPUs.",
                "Install or fix the Nvidia driver and confirm NVML can list the GPU.",
            )
        )
        return InitialCheckResult(None, tuple(issues))

    gpu = _select_gpu(gpu_rows, gpu_index)
    if gpu is None:
        issues.append(
            InitialCheckIssue(
                "error",
                "gpu-index-missing",
                "Selected GPU index is not available",
                f"GPU index {int(gpu_index)} was requested, but NVML returned "
                f"{len(gpu_rows)} GPU(s).",
                "Select an available Nvidia GPU index.",
            )
        )
        return InitialCheckResult(None, tuple(issues))

    architecture_issue = _validate_architecture(gpu.architecture)
    if architecture_issue is not None:
        issues.append(architecture_issue)

    driver_issues = _validate_driver(gpu.driver_version)
    issues.extend(driver_issues)
    if any(issue.severity == "error" for issue in driver_issues):
        return InitialCheckResult(gpu, tuple(issues))

    try:
        client = gpu_client_factory(int(gpu_index))
    except Exception as exc:
        client = None
        client_error = exc
    else:
        client_error = None
    if client is None:
        issues.append(
            InitialCheckIssue(
                "error",
                "nvapi-vf-reader",
                "Daemon GPU client is unavailable",
                "PenguinBurner could not open the daemon GPU API."
                f" Error: {client_error}",
                "Use an RTX 50-series, RTX 40-series, or RTX 30-series card with "
                "driver 580.xx or newer.",
            )
        )
    else:
        issues.extend(_validate_nvapi_voltage_reader(client))
        issues.extend(_validate_vf_curve(client))
        issues.extend(_validate_nvapi_vf_setter(client))
        issues.extend(_validate_nvml_clock_lock(client))
    return InitialCheckResult(gpu, tuple(issues))


def require_auto_uv_initial_check(
    *,
    gpu_index: int = 0,
    log: Callable[[str], None] = print,
) -> InitialCheckResult:
    result = run_auto_uv_initial_check(gpu_index=int(gpu_index))
    if result.ok:
        if result.gpu is not None:
            log(
                "Auto-UV initial check passed: "
                f"gpu={result.gpu.name} "
                f"driver={result.gpu.driver_version} "
                f"architecture={result.gpu.architecture_name}"
            )
        for issue in result.warnings:
            log(f"Auto-UV initial check warning: {issue.title}: {issue.detail}")
        return result
    message = result.format_for_user()
    for line in message.splitlines():
        log(line)
    raise RuntimeError(message)


def _query_nvml_gpus() -> list[InitialCheckGpuInfo]:
    rows = []
    try:
        capabilities = DaemonGpuClient.discover_capabilities()
    except Exception:
        capabilities = []
    for item in capabilities:
        identity = item.identity
        rows.append(
            InitialCheckGpuInfo(
                index=int(identity.index),
                name=identity.name,
                driver_version=identity.driver_version,
                pci_device_id=identity.pci_device_id,
                uuid=identity.uuid,
                architecture=item.architecture,
            )
        )
    return rows


def _select_gpu(
    rows: list[InitialCheckGpuInfo], gpu_index: int
) -> InitialCheckGpuInfo | None:
    for row in rows:
        if int(row.index) == int(gpu_index):
            return row
    return None


def _parse_driver_version(value: str) -> tuple[int, ...]:
    parts = []
    for item in re.findall(r"\d+", str(value)):
        parts.append(int(item))
    return tuple(parts)


def _validate_driver(driver_version: str) -> list[InitialCheckIssue]:
    parsed = _parse_driver_version(driver_version)
    minimum = MINIMUM_NVIDIA_DRIVER_VERSION
    if parsed:
        comparable = (
            int(parsed[0]),
            int(parsed[1]) if len(parsed) > 1 else 0,
        )
    else:
        comparable = ()
    if comparable and comparable >= minimum:
        return []
    found = driver_version or "unknown"
    return [
        InitialCheckIssue(
            "error",
            "driver-version",
            "Nvidia driver is too old",
            f"Found driver {found}; PenguinBurner Auto-UV requires 580.xx or newer.",
            "Install an up-to-date Nvidia proprietary driver and retry.",
        )
    ]


def _validate_architecture(architecture: int | None) -> InitialCheckIssue | None:
    if architecture is None:
        return InitialCheckIssue(
            "warning",
            "nvml-architecture-unavailable",
            "NVML architecture query is unavailable",
            "The Nvidia driver did not report a GPU architecture.",
        )
    try:
        int(architecture)
    except (TypeError, ValueError):
        return InitialCheckIssue(
            "warning",
            "nvml-architecture-failed",
            "NVML architecture query failed",
            f"The hardware service returned {architecture!r}.",
        )
    return None


def _validate_nvapi_voltage_reader(reader: _VoltageReader) -> list[InitialCheckIssue]:
    try:
        voltage_uv = reader.read_microvolts()
    except Exception as exc:
        return [
            InitialCheckIssue(
                "error",
                "nvapi-voltage-reader",
                "NVAPI voltage reader failed",
                str(exc),
                "The driver exposed the voltage query but the call did not "
                "return a usable status.",
            )
        ]
    if voltage_uv is not None:
        return []
    raw_voltage_uv = _reader_last_raw_microvolts(reader)
    raw_detail = (
        f" Raw NVAPI sample: {raw_voltage_uv / 1000.0:.0f}mV."
        if raw_voltage_uv is not None
        else ""
    )
    return [
        InitialCheckIssue(
            "warning",
            "nvapi-voltage-reader",
            "NVAPI voltage reader returned no plausible idle voltage",
            "PenguinBurner could not read a plausible live GPU voltage "
            f"before starting a workload.{raw_detail}",
            "Auto-UV will continue and try to collect voltage telemetry under "
            "load. If it stays unavailable, report the raw sample, driver "
            "version, GPU model, and whether LACT shows voltage on this card.",
        )
    ]


def _validate_vf_curve(reader) -> list[InitialCheckIssue]:
    try:
        points = list(reader.editable_core_points())
    except Exception as exc:
        return [
            InitialCheckIssue(
                "error",
                "nvapi-vf-points",
                "Could not read editable V/F curve points",
                str(exc),
            )
        ]
    issues: list[InitialCheckIssue] = []
    if len(points) < 8:
        issues.append(
            InitialCheckIssue(
                "error",
                "nvapi-vf-point-count",
                "Too few editable V/F curve points",
                f"Found {len(points)} editable voltage-based core points.",
            )
        )
        return issues

    invalid = [
        point
        for point in points
        if int(point.get("voltage_uv", 0) or 0) <= 0
        or int(point.get("freq_khz", 0) or 0) <= 0
        or int(point.get("base_freq_khz", 0) or 0) <= 0
        or int(point.get("base_voltage_uv", 0) or 0) <= 0
    ]
    if invalid:
        issues.append(
            InitialCheckIssue(
                "error",
                "nvapi-vf-invalid-points",
                "Invalid V/F curve points were reported",
                "The V/F curve contains zero or negative voltage/frequency points: "
                f"{_vf_point_sample(invalid)}.",
                "Auto-UV requires a sane editable V/F curve. This is a known "
                "failure mode on older or unsupported GPU/driver combinations.",
            )
        )

    plausible = [
        point
        for point in points
        if 600_000 <= int(point.get("voltage_uv", 0) or 0) <= 1_300_000
        and int(point.get("base_freq_khz", 0) or 0) >= 300_000
    ]
    if len(plausible) < 8:
        issues.append(
            InitialCheckIssue(
                "error",
                "nvapi-vf-implausible",
                "V/F curve does not look usable for Auto-UV",
                "PenguinBurner did not find enough plausible voltage/frequency "
                "points in the 600-1300mV range.",
            )
        )
    return issues


def _validate_nvapi_vf_setter(
    reader: _GpuControl,
) -> list[InitialCheckIssue]:
    try:
        offsets = [
            (int(point["index"]), int(point.get("current_offset_khz", 0) or 0))
            for point in reader.editable_core_points()
        ]
        reader.apply_offsets_khz(offsets)
    except Exception as exc:
        permission_error = _looks_like_permission_error(exc)
        return [
            InitialCheckIssue(
                "error",
                "nvapi-vf-setter",
                "Root daemon was denied access to the NVAPI V/F curve setter"
                if permission_error
                else "Daemon V/F curve setter failed",
                f"Writing the current point offsets back as a no-op failed: {exc}",
                "Restart or reinstall penguin-burnerd; GPU writes must go through "
                "the running root daemon."
                if permission_error
                else "Auto-UV needs the daemon's NVAPI V/F setter before it can "
                "safely edit candidate curves.",
            )
        ]
    return []


def _validate_nvml_clock_lock(
    controller: _GpuControl,
) -> list[InitialCheckIssue]:
    try:
        supported_steps = list(controller.get_supported_core_clock_steps_mhz())
        if not supported_steps:
            return [
                InitialCheckIssue(
                    "error",
                    "nvml-clock-steps",
                    "NVML did not report supported graphics clocks",
                    "PenguinBurner needs supported clock bins before it can lock "
                    "safe probe ceilings.",
                )
            ]
        controller.apply_locked_core_clock_range_mhz(
            min(supported_steps),
            max(supported_steps),
            snap_to_supported=True,
        )
        controller.reset_locked_core_clocks()
    except Exception as exc:
        permission_error = _looks_like_permission_error(exc)
        return [
            InitialCheckIssue(
                "error",
                "nvml-clock-lock",
                "Root daemon was denied access to NVML GPU clock locking"
                if permission_error
                else "NVML GPU clock locking is unsupported",
                str(exc),
                "Restart or reinstall penguin-burnerd; GPU writes must go through "
                "the running root daemon."
                if permission_error
                else "Auto-UV cannot run without nvmlDeviceSetGpuLockedClocks. This "
                "is a common failure mode on older or unsupported GPU/driver "
                "combinations.",
            )
        ]
    finally:
        try:
            controller.reset_locked_core_clocks()
        except Exception:
            pass
    return []


def _reader_last_raw_microvolts(reader) -> int | None:
    getter = getattr(reader, "last_raw_microvolts", None)
    if not callable(getter):
        return None
    try:
        raw = getter()
    except Exception:
        return None
    if raw is None:
        return None
    try:
        return int(cast(Any, raw))
    except (TypeError, ValueError):
        return None


def _format_issue_lines(issues: tuple[InitialCheckIssue, ...]) -> list[str]:
    lines = []
    for issue in issues:
        lines.append(f"- {issue.title}: {issue.detail}")
        if issue.remediation:
            lines.append(f"  Fix: {issue.remediation}")
    return lines


def _vf_point_sample(points: list[dict], *, limit: int = 8) -> str:
    values = []
    for point in points[: int(limit)]:
        values.append(
            f"index={int(point.get('index', -1))} "
            f"voltage={int(point.get('voltage_uv', 0) or 0) // 1000}mV "
            f"freq={int(point.get('freq_khz', 0) or 0) // 1000}MHz "
            f"base={int(point.get('base_freq_khz', 0) or 0) // 1000}MHz"
        )
    if len(points) > int(limit):
        values.append(f"... {len(points) - int(limit)} more")
    return "; ".join(values)


def _looks_like_permission_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        token in text
        for token in (
            "permission",
            "privilege",
            "insufficient permissions",
            "invalid_user_privilege",
            "nvml error 4",
        )
    )
