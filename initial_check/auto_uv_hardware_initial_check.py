"""Check Nvidia driver and GPU controls before Auto-UV starts.

This package is outside auto_uv3 because it validates system readiness, not the undervolt algorithm.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import re
import shutil
import subprocess
from typing import Callable

from hidden_nvapi_vf import (
    create_hidden_vf_curve_reader,
    get_hidden_vf_curve_reader_last_error,
)
from hidden_nvapi_voltage import (
    create_hidden_voltage_reader,
    get_hidden_voltage_reader_last_error,
)
from nvml_gpu_policy import NvmlGpuPolicyController


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
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
    vf_reader_factory: Callable[[int], object | None] = create_hidden_vf_curve_reader,
    gpu_policy_factory: Callable[[int], object] = NvmlGpuPolicyController,
    nvml_library_factory: Callable[[], object] | None = None,
) -> InitialCheckResult:
    issues: list[InitialCheckIssue] = []
    gpu = None
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        issues.append(
            InitialCheckIssue(
                "error",
                "nvidia-smi-missing",
                "nvidia-smi was not found",
                "PenguinBurner could not find the Nvidia driver management tool in PATH.",
                "Install the proprietary Nvidia driver and confirm `nvidia-smi -L` works.",
            )
        )
        return InitialCheckResult(None, tuple(issues))

    gpu_rows, smi_issue = _query_nvidia_smi(nvidia_smi, runner=runner)
    if smi_issue is not None:
        issues.append(smi_issue)
        return InitialCheckResult(None, tuple(issues))
    if not gpu_rows:
        issues.append(
            InitialCheckIssue(
                "error",
                "nvidia-gpu-missing",
                "No Nvidia GPU was detected",
                "`nvidia-smi` did not return any physical GPUs.",
                "Install or fix the Nvidia driver and confirm `nvidia-smi -L` lists the GPU.",
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
                f"GPU index {int(gpu_index)} was requested, but nvidia-smi returned "
                f"{len(gpu_rows)} GPU(s).",
                "Select an available Nvidia GPU index.",
            )
        )
        return InitialCheckResult(None, tuple(issues))

    architecture, architecture_issue = _query_nvml_architecture(
        gpu_index,
        nvml_library_factory=nvml_library_factory,
    )
    if architecture_issue is not None:
        issues.append(architecture_issue)
    gpu = InitialCheckGpuInfo(
        index=gpu.index,
        name=gpu.name,
        driver_version=gpu.driver_version,
        pci_device_id=gpu.pci_device_id,
        uuid=gpu.uuid,
        architecture=architecture,
    )

    driver_issues = _validate_driver(gpu.driver_version)
    issues.extend(driver_issues)
    if any(issue.severity == "error" for issue in driver_issues):
        return InitialCheckResult(gpu, tuple(issues))

    issues.extend(_validate_nvapi_voltage_reader(gpu_index))
    reader_error = None
    try:
        reader = vf_reader_factory(int(gpu_index))
    except Exception as exc:
        reader = None
        reader_error = exc
    if reader is None:
        last_error = reader_error or get_hidden_vf_curve_reader_last_error()
        detail = f" NVAPI error: {last_error}" if last_error is not None else ""
        issues.append(
            InitialCheckIssue(
                "error",
                "nvapi-vf-reader",
                "NVAPI V/F curve reader is unavailable",
                "PenguinBurner could not open the Linux NVAPI V/F helper."
                f"{detail}",
                "Use an RTX 50-series, RTX 40-series, or RTX 30-series card with "
                "driver 580.xx or newer.",
            )
        )
    else:
        try:
            issues.extend(_validate_vf_curve(reader))
            issues.extend(_validate_nvapi_vf_setter(reader))
        finally:
            close = getattr(reader, "close", None)
            if callable(close):
                close()

    issues.extend(_validate_nvml_clock_lock(gpu_index, gpu_policy_factory))
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


def _query_nvidia_smi(
    nvidia_smi: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
) -> tuple[list[InitialCheckGpuInfo], InitialCheckIssue | None]:
    run = runner or subprocess.run
    command = [
        nvidia_smi,
        "--query-gpu=index,name,driver_version,pci.device_id,uuid",
        "--format=csv,noheader,nounits",
    ]
    try:
        result = run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], InitialCheckIssue(
            "error",
            "nvidia-smi-failed",
            "nvidia-smi did not respond",
            str(exc),
            "Fix the Nvidia driver and confirm `nvidia-smi -L` works.",
        )
    if int(result.returncode) != 0:
        output = (result.stdout or result.stderr or "").strip()
        return [], InitialCheckIssue(
            "error",
            "nvidia-smi-failed",
            "nvidia-smi returned an error",
            output or f"exit code {int(result.returncode)}",
            "Fix the Nvidia driver and confirm `nvidia-smi -L` works.",
        )
    rows = []
    for line in str(result.stdout or "").splitlines():
        parts = [part.strip() for part in line.split(",", 4)]
        if len(parts) < 3:
            continue
        try:
            index = int(parts[0])
        except ValueError:
            continue
        rows.append(
            InitialCheckGpuInfo(
                index=index,
                name=parts[1],
                driver_version=parts[2],
                pci_device_id=parts[3] if len(parts) > 3 else "",
                uuid=parts[4] if len(parts) > 4 else "",
            )
        )
    return rows, None


def _select_gpu(rows: list[InitialCheckGpuInfo], gpu_index: int) -> InitialCheckGpuInfo | None:
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


def _query_nvml_architecture(
    gpu_index: int,
    *,
    nvml_library_factory: Callable[[], object] | None = None,
) -> tuple[int | None, InitialCheckIssue | None]:
    try:
        nvml = (
            nvml_library_factory()
            if nvml_library_factory
            else ctypes.CDLL("libnvidia-ml.so.1")
        )
    except OSError as exc:
        return None, InitialCheckIssue(
            "error",
            "nvml-library",
            "NVML library is unavailable",
            str(exc),
            "Install the Nvidia proprietary driver.",
        )
    if not hasattr(nvml, "nvmlDeviceGetArchitecture"):
        return None, InitialCheckIssue(
            "warning",
            "nvml-architecture-unavailable",
            "NVML architecture query is unavailable",
            "The installed NVML library does not expose nvmlDeviceGetArchitecture.",
        )
    try:
        nvml.nvmlInit_v2.restype = ctypes.c_int
        nvml.nvmlShutdown.restype = ctypes.c_int
        nvml.nvmlDeviceGetHandleByIndex_v2.argtypes = [
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        nvml.nvmlDeviceGetHandleByIndex_v2.restype = ctypes.c_int
        nvml.nvmlDeviceGetArchitecture.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint),
        ]
        nvml.nvmlDeviceGetArchitecture.restype = ctypes.c_int
        rc = int(nvml.nvmlInit_v2())
        if rc != 0:
            return None, InitialCheckIssue(
                "error",
                "nvml-init",
                "NVML failed to initialize",
                f"nvmlInit_v2 returned {rc}.",
            )
        device = ctypes.c_void_p()
        rc = int(
            nvml.nvmlDeviceGetHandleByIndex_v2(
                ctypes.c_uint(int(gpu_index)),
                ctypes.byref(device),
            )
        )
        if rc != 0:
            return None, InitialCheckIssue(
                "error",
                "nvml-device",
                "NVML could not open the selected GPU",
                f"nvmlDeviceGetHandleByIndex_v2 returned {rc}.",
            )
        arch = ctypes.c_uint(0)
        rc = int(nvml.nvmlDeviceGetArchitecture(device, ctypes.byref(arch)))
        if rc != 0:
            return None, InitialCheckIssue(
                "warning",
                "nvml-architecture-failed",
                "NVML architecture query failed",
                f"nvmlDeviceGetArchitecture returned {rc}.",
            )
        return int(arch.value), None
    finally:
        try:
            nvml.nvmlShutdown()
        except Exception:
            pass


def _validate_nvapi_voltage_reader(gpu_index: int) -> list[InitialCheckIssue]:
    reader = create_hidden_voltage_reader(gpu_index=int(gpu_index))
    if reader is None:
        last_error = get_hidden_voltage_reader_last_error()
        detail = f" NVAPI error: {last_error}" if last_error is not None else ""
        return [
            InitialCheckIssue(
                "error",
                "nvapi-voltage-reader",
                "NVAPI voltage reader is unavailable",
                "The undocumented Linux NVAPI live voltage query could not be "
                f"resolved.{detail}",
                "Use an RTX 50-series, RTX 40-series, or RTX 30-series card "
                "with driver 580.xx or newer. If this started after a driver "
                "upgrade, report the driver version and GPU model.",
            )
        ]

    try:
        try:
            voltage_uv = reader.read_microvolts()
        except Exception as exc:
            return [
                InitialCheckIssue(
                    "error",
                    "nvapi-voltage-reader",
                    "NVAPI voltage reader failed",
                    str(exc),
                    "The driver exposed the NVAPI voltage query but the call did "
                    "not return a usable status.",
                )
            ]
        if voltage_uv is None:
            return [
                InitialCheckIssue(
                    "error",
                    "nvapi-voltage-reader",
                    "NVAPI voltage reader returned no sane voltage",
                    "PenguinBurner could not read a plausible live GPU voltage.",
                    "This usually means the GPU/driver does not expose the voltage "
                    "getter PenguinBurner needs for Auto-UV.",
                )
            ]
        return []
    finally:
        reader.close()


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


def _validate_nvapi_vf_setter(reader) -> list[InitialCheckIssue]:
    try:
        control = reader.get_control_struct()
        reader.set_control_struct(control)
    except Exception as exc:
        permission_error = _looks_like_permission_error(exc)
        return [
            InitialCheckIssue(
                "error",
                "nvapi-vf-setter",
                "NVAPI V/F curve setter needs elevated privileges"
                if permission_error
                else "NVAPI V/F curve setter failed",
                f"Writing the current V/F control state back as a no-op failed: {exc}",
                "Start Auto Undervolt from the GUI so PenguinBurner can request "
                "privileges, or run the CLI with sudo."
                if permission_error
                else "Auto-UV needs working NVAPI V/F setters before it can safely "
                "edit candidate curves.",
            )
        ]
    return []


def _validate_nvml_clock_lock(
    gpu_index: int,
    gpu_policy_factory: Callable[[int], object],
) -> list[InitialCheckIssue]:
    controller = None
    try:
        controller = gpu_policy_factory(int(gpu_index))
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
                "NVML GPU clock locking needs elevated privileges"
                if permission_error
                else "NVML GPU clock locking is unsupported",
                str(exc),
                "Start Auto Undervolt from the GUI so PenguinBurner can request "
                "privileges, or run the CLI with sudo."
                if permission_error
                else "Auto-UV cannot run without nvmlDeviceSetGpuLockedClocks. This "
                "is a common failure mode on older or unsupported GPU/driver "
                "combinations.",
            )
        ]
    finally:
        if controller is not None:
            try:
                controller.reset_locked_core_clocks()
            except Exception:
                pass
            close = getattr(controller, "close", None)
            if callable(close):
                close()
    return []


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
