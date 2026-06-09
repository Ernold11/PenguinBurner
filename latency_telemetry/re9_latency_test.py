from __future__ import annotations

import argparse
import os
import re
import select
import subprocess
import time
from pathlib import Path
import sys
import threading

from .flow_capture import capture_journal, default_capture_path, write_analysis
from .steam_launch_check import (
    RE9_APP_ID,
    RE9_REQUIRED_TOKENS,
    check_compat_tool,
    check_launch_options,
)
from .steam_re9_setup import (
    RE9_PATCHED_COMPAT_TOOL,
    RE9_PATCHED_EXTRA_TOKENS,
    SteamConfigError,
    apply_patched_re9_setup,
)


RE9_PROCESS_MATCHERS = ("re9.exe", "BIOHAZARD requiem")
RE9_READY_PROCESS_MATCHERS = (
    "S:\\common\\RESIDENT EVIL requiem BIOHAZARD requiem\\re9.exe",
    "\\re9.exe",
)
RE9_READY_PROCESS_ENV_TOKENS = (
    "VK_LOADER_LAYERS_ENABLE=VK_LAYER_PENGUINBURNER_latency,VK_LAYER_DXVK_NVAPI_reflex",
    "PENGUIN_BURNER_LATENCY_QUERY_TIMINGS=0",
    "PENGUIN_BURNER_DXVK_NVAPI_TIMING_QUERY_INTERVAL=4",
    "PENGUIN_BURNER_LATENCY_LAYER=1",
    "DXVK_NVAPI_VKREFLEX=1",
    "VKD3D_LOW_LATENCY_ALLOW_MULTI_SWAPCHAIN=1",
)
KERNEL_FAULT_FILTER_TERMS = (
    "dmaallocmapping",
    "nv_err_no_memory",
    "out of memory",
    "gpu is probably locked",
    "rc watchdog",
    "fallen off the bus",
    "graphics exception",
    "channel exception",
)
NVRM_XID_RE = re.compile(r"\bNVRM:.*\bXid\b|\bXid\b.*\bNVRM:", re.IGNORECASE)


def kernel_journal_command(since: str) -> list[str]:
    command = ["journalctl", "-k", "-o", "short-iso", "--no-pager"]
    if since.strip().lower() in {"now", ""}:
        command.extend(["-n", "0"])
    else:
        command.extend(["--since", since])
    command.append("-f")
    return command


def default_re9_capture_path() -> Path:
    return default_capture_path(prefix="re9-latency-test")


def default_kernel_fault_path(output_path: Path) -> Path:
    return output_path.with_suffix(f"{output_path.suffix}.kernel.log")


def is_kernel_fault_line(line: str) -> bool:
    lowered = line.lower()
    if NVRM_XID_RE.search(line):
        return True
    if any(term in lowered for term in KERNEL_FAULT_FILTER_TERMS):
        return True
    if "nvrm:" in lowered and any(
        term in lowered
        for term in ("error", "failed", "fault", "locked", "hang", "watchdog")
    ):
        return True
    if "nvidia" in lowered and any(
        term in lowered
        for term in (
            "xid",
            "fault",
            "hang",
            "locked",
            "oom",
            "out of memory",
            "reset",
        )
    ):
        return True
    if "gpu" in lowered and any(
        term in lowered
        for term in ("hang", "hung", "locked", "fault", "fallen")
    ):
        return True
    if "re9.exe" in lowered and any(
        term in lowered
        for term in (
            "nvrm",
            "xid",
            "gpu",
            "fault",
            "segfault",
            "trap",
            "oom",
            "out of memory",
            "killed",
            "hang",
            "hung",
            "locked",
        )
    ):
        return True
    return False


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2.0)


def capture_kernel_faults(
    output_path: Path,
    *,
    since: str = "now",
    duration_s: float | None = None,
    sync: bool = True,
    stop_event: threading.Event | None = None,
) -> int:
    command = kernel_journal_command(since)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("", encoding="utf-8")
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
        raise RuntimeError("kernel journal stdout was not captured")

    count = 0
    try:
        with output_path.open("a", encoding="utf-8") as handle:
            while True:
                if stop_event is not None and stop_event.is_set():
                    break
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
                if not is_kernel_fault_line(line):
                    continue
                handle.write(line)
                handle.flush()
                if sync:
                    os.fsync(handle.fileno())
                count += 1
    finally:
        _terminate_process(process)
    return count


def start_kernel_fault_capture(
    output_path: Path,
    *,
    since: str,
    duration_s: float | None,
    sync: bool,
) -> tuple[threading.Thread, threading.Event, dict[str, object]]:
    stop_event = threading.Event()
    result: dict[str, object] = {"count": 0, "error": None}

    def run_capture() -> None:
        try:
            result["count"] = capture_kernel_faults(
                output_path,
                since=since,
                duration_s=duration_s,
                sync=sync,
                stop_event=stop_event,
            )
        except Exception as exc:
            result["error"] = str(exc)

    thread = threading.Thread(target=run_capture, name="re9-kernel-fault-capture")
    thread.start()
    return thread, stop_event, result


def verify_re9_launch_config() -> tuple[bool, str]:
    launch_check = check_launch_options(
        app_id=RE9_APP_ID,
        required_tokens=RE9_REQUIRED_TOKENS + RE9_PATCHED_EXTRA_TOKENS,
    )
    compat_check = check_compat_tool(
        app_id=RE9_APP_ID,
        expected_tool=RE9_PATCHED_COMPAT_TOOL,
    )
    return (
        launch_check.ok and compat_check.ok,
        f"{launch_check.format_text()}\n{compat_check.format_text()}",
    )


def _read_proc_text(path: Path) -> str:
    try:
        return path.read_bytes().replace(b"\0", b"\n").decode(
            "utf-8", errors="replace"
        )
    except OSError:
        return ""


def _re9_process_snapshots(proc_root: Path = Path("/proc")) -> list[dict[str, object]]:
    snapshots: list[dict[str, object]] = []
    for path in proc_root.iterdir():
        if not path.name.isdigit():
            continue
        cmdline = _read_proc_text(path / "cmdline")
        if not any(matcher in cmdline for matcher in RE9_PROCESS_MATCHERS):
            continue
        environ = _read_proc_text(path / "environ")
        snapshots.append(
            {
                "pid": int(path.name),
                "cmdline": " ".join(cmdline.splitlines()),
                "environ": environ,
            }
        )
    return snapshots


def _missing_process_env_tokens(snapshot: dict[str, object]) -> tuple[str, ...]:
    environ = str(snapshot.get("environ") or "")
    return tuple(
        token for token in RE9_READY_PROCESS_ENV_TOKENS if token not in environ
    )


def _is_ready_re9_process(snapshot: dict[str, object]) -> bool:
    cmdline = str(snapshot.get("cmdline") or "")
    return any(matcher in cmdline for matcher in RE9_READY_PROCESS_MATCHERS)


def wait_for_ready_re9_process(
    *,
    timeout_s: float,
    poll_interval_s: float = 1.0,
    proc_root: Path = Path("/proc"),
) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout_s
    last_message = "no RE9 process found"
    while True:
        snapshots = _re9_process_snapshots(proc_root)
        for snapshot in snapshots:
            if not _is_ready_re9_process(snapshot):
                last_message = f"re9_wrapper_pid={snapshot['pid']}"
                continue
            missing = _missing_process_env_tokens(snapshot)
            if not missing:
                return True, f"re9_pid={snapshot['pid']}"
            last_message = (
                f"re9_pid={snapshot['pid']} missing_env="
                f"{','.join(missing)}"
            )

        if time.monotonic() >= deadline:
            return False, last_message
        time.sleep(poll_interval_s)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify the RE9 latency launch config, capture PenguinBurner latency "
            "journal events for a timed test window, and write an analysis file."
        )
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help=(
            "Capture log path. Defaults to "
            "~/.cache/penguin-burner/latency-captures/"
            "re9-latency-test-<timestamp>.log."
        ),
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=180.0,
        help="Seconds to capture. Start or play RE9 during this window.",
    )
    parser.add_argument("--since", default="now")
    parser.add_argument("--service", default="PenguinBurner.service")
    parser.add_argument(
        "--wait-for-re9",
        action="store_true",
        help="Wait for a RE9 process with the diagnostic env before capturing.",
    )
    parser.add_argument(
        "--process-timeout",
        type=float,
        default=300.0,
        help="Seconds to wait with --wait-for-re9.",
    )
    parser.add_argument(
        "--apply-setup",
        action="store_true",
        help="Apply the patched RE9 Steam launch config before verifying.",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="With --apply-setup, wait for Steam/Wine processes to exit.",
    )
    parser.add_argument(
        "--allow-bad-launch",
        action="store_true",
        help="Capture even if the Steam launch config is missing required tokens.",
    )
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="Do not fsync every captured line.",
    )
    parser.add_argument(
        "--kernel-output",
        type=Path,
        default=None,
        help=(
            "Kernel fault sidecar path. Defaults to the capture path with "
            ".kernel.log appended."
        ),
    )
    parser.add_argument(
        "--no-kernel-log",
        action="store_true",
        help="Do not capture NVRM/Xid/kernel GPU fault lines alongside latency.",
    )
    args = parser.parse_args(argv)

    if args.apply_setup:
        try:
            result = apply_patched_re9_setup(wait=args.wait)
        except SteamConfigError as exc:
            print(f"setup_error={exc}")
            return 2
        print(result.format_text())

    launch_ok, launch_text = verify_re9_launch_config()
    print(launch_text)
    if not launch_ok and not args.allow_bad_launch:
        print("error=RE9 launch config is not ready for the latency test")
        return 2

    if args.wait_for_re9:
        ready, message = wait_for_ready_re9_process(timeout_s=args.process_timeout)
        print(message)
        if not ready:
            print("error=ready RE9 process was not observed")
            return 2

    output_path = args.output or default_re9_capture_path()
    kernel_path = args.kernel_output or default_kernel_fault_path(output_path)
    kernel_thread: threading.Thread | None = None
    kernel_stop: threading.Event | None = None
    kernel_result: dict[str, object] = {"count": 0, "error": None}
    if not args.no_kernel_log:
        kernel_thread, kernel_stop, kernel_result = start_kernel_fault_capture(
            kernel_path,
            since=args.since,
            duration_s=args.duration + 5.0 if args.duration is not None else None,
            sync=not args.no_sync,
        )

    interrupted = False
    try:
        count = capture_journal(
            output_path,
            service=args.service,
            since=args.since,
            follow=True,
            duration_s=args.duration,
            append=False,
            sync=not args.no_sync,
        )
    except KeyboardInterrupt:
        interrupted = True
        count = 0
        if output_path.exists():
            count = sum(
                1 for _line in output_path.read_text(encoding="utf-8").splitlines()
            )
        print("interrupted=True")
    finally:
        if kernel_stop is not None:
            kernel_stop.set()
        if kernel_thread is not None:
            kernel_thread.join(timeout=3.0)

    analysis_path = write_analysis(output_path)
    print(f"capture={output_path}")
    print(f"analysis={analysis_path}")
    print(f"captured_lines={count}")
    if not args.no_kernel_log:
        print(f"kernel_capture={kernel_path}")
        print(f"kernel_fault_lines={kernel_result.get('count', 0)}")
        if kernel_result.get("error"):
            print(f"kernel_capture_error={kernel_result['error']}")
    sys.stdout.write(analysis_path.read_text(encoding="utf-8"))
    return 130 if interrupted else 0


if __name__ == "__main__":
    raise SystemExit(main())
