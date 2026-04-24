from __future__ import annotations

from .assets import (
    resolve_q2rtx_executable,
    resolve_q2rtx_workdir,
    resolve_workload,
)
from .cli import config_from_args, main, parse_q2rtx_stability_args
from .constants import (
    DEFAULT_DEMO_NAME,
    DEFAULT_DURATION_S,
    DEFAULT_HIDE_WINDOW,
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
    default_q2rtx_compat_dir,
    default_q2rtx_install_cache_dir,
    default_q2rtx_install_data_dir,
    fetch_latest_q2rtx_release_metadata,
    install_latest_q2rtx,
)
from .models import (
    Q2RTXInstallResult,
    Q2RTXStabilityConfig,
    Q2RTXStabilityResult,
    StabilityTestError,
    TelemetrySample,
    TimedemoRun,
)
from .output import attach_stdout_progress
from .reporting import print_q2rtx_stability_result
from .runtime import (
    build_timedemo_command,
    cleanup_managed_q2rtx_processes,
    run_q2rtx_stability_test,
)
from .telemetry import query_gpu_metrics

__all__ = [
    "DEFAULT_DEMO_NAME",
    "DEFAULT_DURATION_S",
    "DEFAULT_HIDE_WINDOW",
    "DEFAULT_HEIGHT",
    "DEFAULT_INSTALL_CACHE_DIR",
    "DEFAULT_INSTALL_DATA_DIR",
    "DEFAULT_LOG_DIR",
    "DEFAULT_POLL_INTERVAL_S",
    "DEFAULT_SINGLE_PASS_TIMEOUT_S",
    "DEFAULT_WIDTH",
    "KNOWN_TIMEDEMO_FRAME_COUNTS",
    "KNOWN_TIMEDEMO_RUN_SECONDS_HINTS",
    "Q2RTXInstallResult",
    "Q2RTXStabilityConfig",
    "Q2RTXStabilityResult",
    "StabilityTestError",
    "TelemetrySample",
    "TimedemoRun",
    "attach_stdout_progress",
    "build_timedemo_command",
    "cleanup_managed_q2rtx_processes",
    "config_from_args",
    "default_q2rtx_compat_dir",
    "default_q2rtx_install_cache_dir",
    "default_q2rtx_install_data_dir",
    "fetch_latest_q2rtx_release_metadata",
    "install_latest_q2rtx",
    "main",
    "parse_q2rtx_stability_args",
    "print_q2rtx_stability_result",
    "query_gpu_metrics",
    "resolve_q2rtx_executable",
    "resolve_q2rtx_workdir",
    "resolve_workload",
    "run_q2rtx_stability_test",
]
