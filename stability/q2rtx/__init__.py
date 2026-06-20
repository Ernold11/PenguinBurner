from __future__ import annotations

from importlib import import_module

from .assets import (
    resolve_q2rtx_executable,
    resolve_workload,
)
from .constants import (
    DEFAULT_DEMO_NAME,
    DEFAULT_DURATION_S,
    DEFAULT_HEIGHT,
    DEFAULT_INSTALL_CACHE_DIR,
    DEFAULT_INSTALL_DATA_DIR,
    DEFAULT_LOG_DIR,
    DEFAULT_POLL_INTERVAL_S,
    DEFAULT_SINGLE_PASS_TIMEOUT_S,
    DEFAULT_WIDTH,
    KNOWN_TIMEDEMO_FRAME_COUNTS,
    KNOWN_TIMEDEMO_RUN_SECONDS_HINTS,
)
from .install import (
    default_q2rtx_install_cache_dir,
    default_q2rtx_install_data_dir,
    fetch_latest_q2rtx_release_metadata,
    install_latest_q2rtx,
)
from .models import (
    Q2RTXInstallResult,
    Q2RTXBenchmarkSummary,
    Q2RTXStabilityConfig,
    Q2RTXStabilityResult,
    StabilityTestError,
    TelemetrySample,
)
from .output import attach_stdout_progress
from .process_harness import cleanup_managed_q2rtx_processes
from .reporting import print_q2rtx_stability_result

_LAZY_EXPORTS = {
    "build_long_stability_test_config": (
        ".long_stability_config",
        "build_long_stability_test_config",
    ),
    "build_benchmark_command": (".runtime", "build_benchmark_command"),
    "config_from_args": (".cli", "config_from_args"),
    "long_stability_workload_durations": (
        ".long_stability_config",
        "long_stability_workload_durations",
    ),
    "main": (".cli", "main"),
    "parse_q2rtx_stability_args": (".cli", "parse_q2rtx_stability_args"),
    "query_gpu_metrics": (".telemetry", "query_gpu_metrics"),
    "run_cuda_stability_test": (".runtime", "run_cuda_stability_test"),
    "run_q2rtx_stability_test": (".runtime", "run_q2rtx_stability_test"),
}


def __getattr__(name: str):
    export = _LAZY_EXPORTS.get(name)
    if export is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = export
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value

__all__ = [
    "DEFAULT_DEMO_NAME",
    "DEFAULT_DURATION_S",
    "DEFAULT_HEIGHT",
    "DEFAULT_INSTALL_CACHE_DIR",
    "DEFAULT_INSTALL_DATA_DIR",
    "DEFAULT_LOG_DIR",
    "DEFAULT_POLL_INTERVAL_S",
    "DEFAULT_SINGLE_PASS_TIMEOUT_S",
    "DEFAULT_WIDTH",
    "KNOWN_TIMEDEMO_FRAME_COUNTS",
    "KNOWN_TIMEDEMO_RUN_SECONDS_HINTS",
    "Q2RTXBenchmarkSummary",
    "Q2RTXInstallResult",
    "Q2RTXStabilityConfig",
    "Q2RTXStabilityResult",
    "StabilityTestError",
    "TelemetrySample",
    "attach_stdout_progress",
    "build_benchmark_command",
    "build_long_stability_test_config",
    "cleanup_managed_q2rtx_processes",
    "config_from_args",
    "default_q2rtx_install_cache_dir",
    "default_q2rtx_install_data_dir",
    "fetch_latest_q2rtx_release_metadata",
    "install_latest_q2rtx",
    "long_stability_workload_durations",
    "main",
    "parse_q2rtx_stability_args",
    "print_q2rtx_stability_result",
    "query_gpu_metrics",
    "resolve_q2rtx_executable",
    "resolve_workload",
    "run_cuda_stability_test",
    "run_q2rtx_stability_test",
]
