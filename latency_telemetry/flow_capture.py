from __future__ import annotations

from datetime import datetime
import argparse
import os
from pathlib import Path
import select
import subprocess
import sys
import time
from typing import Iterable

from .flow_analysis import analyze_latency_flow_lines


FLOW_FILTER_TERMS = (
    "create-swapchain",
    "destroy-swapchain",
    "latency-stream-stale",
    "present-flow",
    "latency-sleep",
    "latency-queue-out-of-band",
    "latency-raw",
    "latency-meter",
)


def is_latency_flow_line(line: str) -> bool:
    return any(term in line for term in FLOW_FILTER_TERMS)


def default_capture_path(
    *,
    now: datetime | None = None,
    base_dir: Path | None = None,
    prefix: str = "latency-flow",
) -> Path:
    timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    root = base_dir or Path.home() / ".cache" / "penguin-burner" / "latency-captures"
    return root / f"{prefix}-{timestamp}.log"


def write_filtered_lines(
    lines: Iterable[str],
    output_path: Path,
    *,
    append: bool = False,
    sync: bool = True,
) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    mode = "a" if append else "w"
    with output_path.open(mode, encoding="utf-8") as handle:
        for line in lines:
            if not is_latency_flow_line(line):
                continue
            if not line.endswith("\n"):
                line = f"{line}\n"
            handle.write(line)
            handle.flush()
            if sync:
                os.fsync(handle.fileno())
            count += 1
    return count


def write_analysis(output_path: Path, analysis_path: Path | None = None) -> Path:
    if analysis_path is None:
        analysis_path = output_path.with_suffix(f"{output_path.suffix}.analysis.txt")
    lines = output_path.read_text(encoding="utf-8", errors="replace").splitlines()
    diagnosis = analyze_latency_flow_lines(lines)
    analysis_path.write_text(f"{diagnosis.format_text()}\n", encoding="utf-8")
    return analysis_path


def _ensure_capture_file(output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.touch(exist_ok=True)


def _prepare_capture_file(output_path: Path, *, append: bool) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if append:
        output_path.touch(exist_ok=True)
    else:
        output_path.write_text("", encoding="utf-8")


def _captured_line_count(output_path: Path) -> int:
    if not output_path.exists():
        return 0
    with output_path.open("r", encoding="utf-8", errors="replace") as handle:
        return sum(1 for _line in handle)


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2.0)


def capture_journal(
    output_path: Path,
    *,
    service: str = "PenguinBurner.service",
    since: str = "now",
    follow: bool = True,
    duration_s: float | None = None,
    append: bool = False,
    sync: bool = True,
) -> int:
    command = [
        "journalctl",
        "-u",
        service,
        "--since",
        since,
        "-o",
        "cat",
        "--no-pager",
    ]
    if follow:
        command.append("-f")

    _prepare_capture_file(output_path, append=append)
    deadline = time.monotonic() + duration_s if duration_s is not None else None
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    if process.stdout is None:
        _terminate_process(process)
        raise RuntimeError("journalctl stdout was not captured")

    count = 0
    try:
        with output_path.open("a", encoding="utf-8") as handle:
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                timeout = 0.25
                if deadline is not None:
                    timeout = max(0.0, min(timeout, deadline - time.monotonic()))
                ready, _, _ = select.select([process.stdout], [], [], timeout)
                if not ready:
                    if process.poll() is not None:
                        break
                    continue
                line = process.stdout.readline()
                if not line:
                    if process.poll() is not None:
                        break
                    continue
                if not is_latency_flow_line(line):
                    continue
                handle.write(line)
                handle.flush()
                if sync:
                    os.fsync(handle.fileno())
                count += 1
    finally:
        _terminate_process(process)
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture PenguinBurner latency flow journal lines to a durable log."
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Capture log path. Defaults to ~/.cache/penguin-burner/latency-captures/latency-flow-<timestamp>.log.",
    )
    parser.add_argument("--service", default="PenguinBurner.service")
    parser.add_argument("--since", default="now")
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Stop after this many seconds. Without this, follow until interrupted.",
    )
    parser.add_argument(
        "--no-follow",
        action="store_true",
        help="Read existing journal lines and exit instead of following.",
    )
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="Do not fsync every captured line.",
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="Append to an existing capture log instead of replacing it.",
    )
    args = parser.parse_args(argv)

    output_path = args.output or default_capture_path()
    interrupted = False
    try:
        count = capture_journal(
            output_path,
            service=args.service,
            since=args.since,
            follow=not args.no_follow,
            duration_s=args.duration,
            append=args.append,
            sync=not args.no_sync,
        )
    except KeyboardInterrupt:
        interrupted = True
        _ensure_capture_file(output_path)
        count = _captured_line_count(output_path)

    analysis_path = write_analysis(output_path)
    print(f"capture={output_path}")
    print(f"analysis={analysis_path}")
    print(f"captured_lines={count}")
    if interrupted:
        print("interrupted=True")
    sys.stdout.write(analysis_path.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
