from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .assets import _validate_demo_name
from .constants import (
    DEFAULT_DEMO_NAME,
    DEFAULT_DURATION_S,
    DEFAULT_HEIGHT,
    DEFAULT_LOG_DIR,
    DEFAULT_WIDTH,
)
from .install import install_latest_q2rtx
from .models import Q2RTXStabilityConfig, StabilityTestError
from .output import attach_stdout_progress
from .reporting import print_q2rtx_stability_result
from .runtime import run_q2rtx_stability_test


def _default_prog_name() -> str:
    invoked_name = Path(sys.argv[0]).name
    if invoked_name == "__main__.py":
        return "python -m stability.q2rtx"
    return invoked_name or "q2rtx_stability.py"


def parse_q2rtx_stability_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=_default_prog_name(),
        description="Run a non-interactive Q2RTX timedemo stability workload.",
    )
    parser.add_argument(
        "--install-q2rtx",
        action="store_true",
        help=(
            "Download the latest official Q2RTX Linux tar.gz from GitHub and "
            "extract everything under ~/.local/share/PenguinBurner/q2rtx/"
        ),
    )
    parser.add_argument(
        "--stability-seconds",
        type=int,
        default=DEFAULT_DURATION_S,
        help=(
            "Wall-clock duration in seconds when --stability-loops is not set; "
            f"default {DEFAULT_DURATION_S}"
        ),
    )
    parser.add_argument(
        "--stability-q2rtx-dir",
        default="",
        help="Q2RTX install or source root containing baseq2/ and/or q2rtx",
    )
    parser.add_argument(
        "--stability-q2rtx-binary",
        default="",
        help="Explicit q2rtx executable path",
    )
    parser.add_argument(
        "--stability-demo",
        default=DEFAULT_DEMO_NAME,
        help=(
            "Demo name under baseq2/demos or inside pak0.pak, or 'auto' to "
            f"prefer a built-in timedemo like q2demo1; default {DEFAULT_DEMO_NAME}"
        ),
    )
    parser.add_argument(
        "--stability-loops",
        type=int,
        default=0,
        help=(
            "Exact built-in timedemo loop count; when set, skip wall-clock "
            "calibration and run one timedemo process for this many loops"
        ),
    )
    parser.add_argument(
        "--stability-width",
        type=int,
        default=DEFAULT_WIDTH,
        help=f"Q2RTX render width; default {DEFAULT_WIDTH}",
    )
    parser.add_argument(
        "--stability-height",
        type=int,
        default=DEFAULT_HEIGHT,
        help=f"Q2RTX render height; default {DEFAULT_HEIGHT}",
    )
    parser.add_argument(
        "--show-q2rtx-window",
        action="store_true",
        help=(
            "Do not move the Q2RTX Vulkan window off-screen during the stability test"
        ),
    )
    parser.add_argument(
        "--gpu-index",
        type=int,
        default=0,
        help="GPU index for telemetry polling; default 0",
    )
    parser.add_argument(
        "--stability-log-dir",
        default=str(DEFAULT_LOG_DIR),
        help=f"Directory for captured logs; default {DEFAULT_LOG_DIR}",
    )
    return parser.parse_args(argv)


def config_from_args(args: argparse.Namespace) -> Q2RTXStabilityConfig:
    q2rtx_dir = str(args.stability_q2rtx_dir).strip()
    q2rtx_binary = str(args.stability_q2rtx_binary).strip()
    return Q2RTXStabilityConfig(
        duration_s=int(args.stability_seconds),
        width=int(args.stability_width),
        height=int(args.stability_height),
        hide_window=not bool(args.show_q2rtx_window),
        demo_name=_validate_demo_name(args.stability_demo),
        timedemo_loops=(
            int(args.stability_loops) if int(args.stability_loops) > 0 else None
        ),
        gpu_index=int(args.gpu_index),
        q2rtx_dir=Path(q2rtx_dir).expanduser() if q2rtx_dir else None,
        q2rtx_binary=Path(q2rtx_binary).expanduser() if q2rtx_binary else None,
        log_dir=Path(args.stability_log_dir).expanduser(),
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_q2rtx_stability_args(argv)
    try:
        if args.install_q2rtx:
            result = install_latest_q2rtx()
            print(
                f"Installed Q2RTX {result.version} to {result.install_dir}",
                flush=True,
            )
            print(f"Executable: {result.executable_path}", flush=True)
            print(f"Archive cache: {result.archive_path}", flush=True)
            print(f"Source: {result.asset_url}", flush=True)
            return 0
        config = config_from_args(args)
        attach_stdout_progress(config)
        result = run_q2rtx_stability_test(config)
    except StabilityTestError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 2
    except OSError as exc:
        print(f"error: failed to start Q2RTX: {exc}", file=sys.stderr, flush=True)
        return 2

    print_q2rtx_stability_result(result)
    return 0 if result.success else 1
