from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import sys
from typing import Any


DEFAULT_DAEMON_SOCKET = "/run/penguin-burnerd.sock"

# Overrides the daemon socket for the milestone-B client wrappers below (GPU
# writes, profile verification, profile deletion). Production leaves it unset
# (the daemon always listens on DEFAULT_DAEMON_SOCKET); tests point it at a
# temp socket, and a daemon-spawned child could inherit it for a nonstandard
# --socket setup.
DAEMON_SOCKET_ENV = "PENGUIN_BURNER_DAEMON_SOCKET"

# GPU writes ride slow NVML/NVAPI driver calls (supported-clock enumeration,
# VF-curve get-mutate-set) and serialize under the daemon's backend mutex, so
# they get a far larger budget than the 3 s control-plane default.
GPU_WRITE_TIMEOUT_S = 30.0


def _resolved_socket_path(socket_path: str | Path | None) -> str | Path:
    if socket_path is not None:
        return socket_path
    return os.environ.get(DAEMON_SOCKET_ENV, "").strip() or DEFAULT_DAEMON_SOCKET


def daemon_request(
    method: str,
    *,
    socket_path: str | Path = DEFAULT_DAEMON_SOCKET,
    timeout_s: float = 3.0,
) -> dict[str, Any]:
    return daemon_payload_request(
        {"method": str(method)},
        socket_path=socket_path,
        timeout_s=timeout_s,
    )


def daemon_payload_request(
    request: dict[str, Any],
    *,
    socket_path: str | Path = DEFAULT_DAEMON_SOCKET,
    timeout_s: float = 3.0,
) -> dict[str, Any]:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(float(timeout_s))
            client.connect(str(socket_path))
            client.sendall(
                (json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8")
            )
            chunks: list[bytes] = []
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
    except FileNotFoundError as exc:
        raise RuntimeError(f"PenguinBurner daemon socket not found: {socket_path}") from exc
    except OSError as exc:
        raise RuntimeError(f"failed to connect to PenguinBurner daemon: {exc}") from exc

    line = b"".join(chunks).split(b"\n", 1)[0].decode("utf-8", errors="replace")
    if not line:
        raise RuntimeError("PenguinBurner daemon returned an empty response")
    response = json.loads(line)
    if not isinstance(response, dict):
        raise RuntimeError("PenguinBurner daemon returned an invalid response")
    if not response.get("ok"):
        raise RuntimeError(
            str(response.get("error") or "PenguinBurner daemon request failed")
        )
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("PenguinBurner daemon returned an invalid result")
    return result


def daemon_stream_request(
    request: dict[str, Any],
    *,
    socket_path: str | Path = DEFAULT_DAEMON_SOCKET,
    timeout_s: float = 3.0,
):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(float(timeout_s))
            client.connect(str(socket_path))
            client.settimeout(None)
            client.sendall(
                (json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8")
            )
            buffer = b""
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    if buffer.strip():
                        yield _decode_response_line(buffer)
                    return
                buffer += chunk
                while b"\n" in buffer:
                    raw_line, buffer = buffer.split(b"\n", 1)
                    if raw_line.strip():
                        yield _decode_response_line(raw_line)
    except FileNotFoundError as exc:
        raise RuntimeError(f"PenguinBurner daemon socket not found: {socket_path}") from exc
    except OSError as exc:
        raise RuntimeError(f"failed to connect to PenguinBurner daemon: {exc}") from exc


def daemon_status(
    *,
    socket_path: str | Path = DEFAULT_DAEMON_SOCKET,
    timeout_s: float = 3.0,
) -> dict[str, Any]:
    return daemon_request("status", socket_path=socket_path, timeout_s=timeout_s)


def probe_power_limit_support(
    gpu_index: int,
    *,
    socket_path: str | Path | None = None,
    timeout_s: float = 1.0,
) -> dict[str, Any]:
    return daemon_payload_request(
        {"method": "probe_power_limit_support", "gpu_index": int(gpu_index)},
        socket_path=_resolved_socket_path(socket_path),
        timeout_s=timeout_s,
    )


def start_runtime_profile(
    argv: list[str],
    *,
    socket_path: str | Path = DEFAULT_DAEMON_SOCKET,
    timeout_s: float = 3.0,
) -> dict[str, Any]:
    return daemon_payload_request(
        {"method": "start_runtime_profile", "argv": list(argv)},
        socket_path=socket_path,
        timeout_s=timeout_s,
    )


def stop_runtime_profile(
    *,
    socket_path: str | Path = DEFAULT_DAEMON_SOCKET,
    timeout_s: float = 3.0,
) -> dict[str, Any]:
    return daemon_request(
        "stop_runtime_profile",
        socket_path=socket_path,
        timeout_s=timeout_s,
    )


# --- GPU write RPCs (milestone B) ------------------------------------------
#
# Thin wrappers over the Rust daemon's gpu_* methods (burnerd/src/gpu_rpc.rs).
# The daemon relays the backend driver's exact error text in the {"ok":false,
# "error":...} envelope, which daemon_payload_request raises verbatim as the
# RuntimeError message -- consumers pattern-match those strings, so nothing may
# rephrase them.


def _gpu_request(
    method: str,
    gpu_index,
    extra: dict[str, Any],
    *,
    socket_path: str | Path | None,
    timeout_s: float,
) -> dict[str, Any]:
    return daemon_payload_request(
        {"method": method, "gpu_index": int(gpu_index), **extra},
        socket_path=_resolved_socket_path(socket_path),
        timeout_s=timeout_s,
    )


def gpu_apply_vf_offsets(
    gpu_index,
    offsets,
    *,
    socket_path: str | Path | None = None,
    timeout_s: float = GPU_WRITE_TIMEOUT_S,
) -> dict[str, Any]:
    """Apply a VF plan of ``(index, offset_khz)`` pairs.

    The daemon owns the NVAPI get-mutate-set cycle: it reads the live control
    struct, overwrites ``freq_offset_khz`` for exactly the listed indices
    (preserving every non-listed point), and sets the result.
    """
    plan = [[int(index), int(offset_khz)] for index, offset_khz in offsets]
    return _gpu_request(
        "gpu_apply_vf_offsets",
        gpu_index,
        {"offsets": plan},
        socket_path=socket_path,
        timeout_s=timeout_s,
    )


def gpu_apply_power_limit(
    gpu_index,
    power_limit_w,
    *,
    socket_path: str | Path | None = None,
    timeout_s: float = GPU_WRITE_TIMEOUT_S,
) -> dict[str, Any]:
    return _gpu_request(
        "gpu_apply_power_limit",
        gpu_index,
        {"power_limit_w": int(power_limit_w)},
        socket_path=socket_path,
        timeout_s=timeout_s,
    )


def gpu_apply_clock_offsets(
    gpu_index,
    *,
    gpc_clk_vf_offset_mhz=None,
    mem_clk_vf_offset_mhz=None,
    socket_path: str | Path | None = None,
    timeout_s: float = GPU_WRITE_TIMEOUT_S,
) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    if gpc_clk_vf_offset_mhz is not None:
        extra["gpc_clk_vf_offset_mhz"] = int(gpc_clk_vf_offset_mhz)
    if mem_clk_vf_offset_mhz is not None:
        extra["mem_clk_vf_offset_mhz"] = int(mem_clk_vf_offset_mhz)
    return _gpu_request(
        "gpu_apply_clock_offsets",
        gpu_index,
        extra,
        socket_path=socket_path,
        timeout_s=timeout_s,
    )


def gpu_apply_locked_core_clock(
    gpu_index,
    clock_mhz,
    *,
    prefer_not_above: bool = True,
    snap_to_supported: bool = True,
    socket_path: str | Path | None = None,
    timeout_s: float = GPU_WRITE_TIMEOUT_S,
) -> dict[str, Any]:
    return _gpu_request(
        "gpu_apply_locked_core_clock",
        gpu_index,
        {
            "clock_mhz": int(clock_mhz),
            "prefer_not_above": bool(prefer_not_above),
            "snap_to_supported": bool(snap_to_supported),
        },
        socket_path=socket_path,
        timeout_s=timeout_s,
    )


def gpu_apply_locked_core_clock_range(
    gpu_index,
    min_clock_mhz,
    max_clock_mhz,
    *,
    prefer_max_not_above: bool = True,
    snap_to_supported: bool = True,
    socket_path: str | Path | None = None,
    timeout_s: float = GPU_WRITE_TIMEOUT_S,
) -> dict[str, Any]:
    return _gpu_request(
        "gpu_apply_locked_core_clock_range",
        gpu_index,
        {
            "min_mhz": int(min_clock_mhz),
            "max_mhz": int(max_clock_mhz),
            "prefer_max_not_above": bool(prefer_max_not_above),
            "snap_to_supported": bool(snap_to_supported),
        },
        socket_path=socket_path,
        timeout_s=timeout_s,
    )


def gpu_reset_locked_core_clocks(
    gpu_index,
    *,
    socket_path: str | Path | None = None,
    timeout_s: float = GPU_WRITE_TIMEOUT_S,
) -> dict[str, Any]:
    return _gpu_request(
        "gpu_reset_locked_core_clocks",
        gpu_index,
        {},
        socket_path=socket_path,
        timeout_s=timeout_s,
    )


def gpu_reset_locked_memory_clocks(
    gpu_index,
    *,
    socket_path: str | Path | None = None,
    timeout_s: float = GPU_WRITE_TIMEOUT_S,
) -> dict[str, Any]:
    return _gpu_request(
        "gpu_reset_locked_memory_clocks",
        gpu_index,
        {},
        socket_path=socket_path,
        timeout_s=timeout_s,
    )


def gpu_enable_persistence_mode(
    gpu_index,
    *,
    socket_path: str | Path | None = None,
    timeout_s: float = GPU_WRITE_TIMEOUT_S,
) -> dict[str, Any]:
    return _gpu_request(
        "gpu_enable_persistence_mode",
        gpu_index,
        {},
        socket_path=socket_path,
        timeout_s=timeout_s,
    )


def delete_auto_uv_profiles(
    profile_paths,
    *,
    socket_path: str | Path | None = None,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    """Ask the root daemon to delete saved Auto-UV profile files.

    The daemon canonicalizes and prefix-enforces every path against the
    effective user's ``~/.config/PenguinBurner/auto-uv-profiles`` dir; any
    rejected path fails the whole request with nothing deleted.
    """
    return daemon_payload_request(
        {
            "method": "delete_auto_uv_profiles",
            "paths": [str(path) for path in profile_paths],
        },
        socket_path=_resolved_socket_path(socket_path),
        timeout_s=timeout_s,
    )


def stop_profile_verification(
    *,
    socket_path: str | Path | None = None,
    timeout_s: float = 3.0,
) -> dict[str, Any]:
    return daemon_request(
        "stop_profile_verification",
        socket_path=_resolved_socket_path(socket_path),
        timeout_s=timeout_s,
    )


def stream_profile_verification(
    options: dict[str, Any],
    *,
    socket_path: str | Path | None = None,
    stdout=None,
    stderr=None,
) -> int:
    """Run a daemon-side profile verification, relaying its output lines.

    Mirrors stream_auto_uv_scan: SIGINT converts into a cooperative
    stop_profile_verification request instead of killing the stream.
    """
    socket_path = _resolved_socket_path(socket_path)
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr

    def _request_stop(_signum, _frame):
        try:
            stop_profile_verification(socket_path=socket_path, timeout_s=1.0)
        except Exception as exc:
            print(
                f"warning: failed to request profile verification stop: {exc}",
                file=stderr,
                flush=True,
            )

    previous_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, _request_stop)
    exit_code = 1
    try:
        for payload in daemon_stream_request(
            {"method": "start_profile_verification", "options": options},
            socket_path=socket_path,
        ):
            if not payload.get("ok"):
                print(
                    str(payload.get("error") or "profile verification daemon request failed"),
                    file=stderr,
                    flush=True,
                )
                return 1
            line = payload.get("line")
            if isinstance(line, str):
                print(line, end="", file=stdout, flush=True)
                continue
            if payload.get("control") == "finished":
                exit_code = int(payload.get("exit_code", 1))
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
    return int(exit_code)


def stream_auto_uv_scan(
    options: dict[str, Any],
    *,
    socket_path: str | Path = DEFAULT_DAEMON_SOCKET,
    stdout=None,
    stderr=None,
) -> int:
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr

    def _request_stop(_signum, _frame):
        try:
            daemon_request("stop_auto_uv_scan", socket_path=socket_path, timeout_s=1.0)
        except Exception as exc:
            print(f"warning: failed to request Auto-UV stop: {exc}", file=stderr, flush=True)

    previous_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, _request_stop)
    exit_code = 1
    try:
        for payload in daemon_stream_request(
            {"method": "start_auto_uv_scan", "options": options},
            socket_path=socket_path,
        ):
            if not payload.get("ok"):
                print(str(payload.get("error") or "Auto-UV daemon request failed"), file=stderr, flush=True)
                return 1
            line = payload.get("line")
            if isinstance(line, str):
                print(line, end="", file=stdout, flush=True)
                continue
            if payload.get("control") == "finished":
                exit_code = int(payload.get("exit_code", 1))
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
    return int(exit_code)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PenguinBurner daemon client")
    parser.add_argument(
        "--socket",
        default=None,
        help=argparse.SUPPRESS,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    subparsers.add_parser("stop-auto-uv")
    subparsers.add_parser("stop-runtime-profile")
    subparsers.add_parser("stop-profile-verification")
    start = subparsers.add_parser("start-auto-uv")
    start.add_argument("options_json")
    runtime = subparsers.add_parser("start-runtime-profile")
    runtime.add_argument("argv_json")
    verify = subparsers.add_parser("start-profile-verification")
    verify.add_argument("options_json")
    delete = subparsers.add_parser("delete-auto-uv-profiles")
    delete.add_argument("paths_json")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    # No explicit --socket: honor the env override, else the default socket.
    args.socket = _resolved_socket_path(args.socket)

    try:
        if args.command == "status":
            print(
                json.dumps(daemon_status(socket_path=args.socket), indent=2),
                flush=True,
            )
            return 0
        if args.command == "stop-auto-uv":
            print(
                json.dumps(
                    daemon_request("stop_auto_uv_scan", socket_path=args.socket),
                    indent=2,
                ),
                flush=True,
            )
            return 0
        if args.command == "stop-runtime-profile":
            print(
                json.dumps(stop_runtime_profile(socket_path=args.socket), indent=2),
                flush=True,
            )
            return 0
        if args.command == "stop-profile-verification":
            print(
                json.dumps(
                    stop_profile_verification(socket_path=args.socket),
                    indent=2,
                ),
                flush=True,
            )
            return 0
        if args.command == "start-auto-uv":
            options = json.loads(args.options_json)
            if not isinstance(options, dict):
                raise RuntimeError("Auto-UV options JSON must be an object")
            return stream_auto_uv_scan(options, socket_path=args.socket)
        if args.command == "start-profile-verification":
            options = json.loads(args.options_json)
            if not isinstance(options, dict):
                raise RuntimeError("profile verification options JSON must be an object")
            return stream_profile_verification(options, socket_path=args.socket)
        if args.command == "delete-auto-uv-profiles":
            paths = json.loads(args.paths_json)
            if not isinstance(paths, list) or not all(
                isinstance(item, str) for item in paths
            ):
                raise RuntimeError("profile paths JSON must be a string list")
            result = delete_auto_uv_profiles(paths, socket_path=args.socket)
            print(json.dumps(result, indent=2), flush=True)
            deleted = result.get("deleted")
            count = len(deleted) if isinstance(deleted, list) else 0
            label = "profile" if count == 1 else "profiles"
            print(f"Deleted {count} saved Auto-UV {label}.", flush=True)
            return 0
        if args.command == "start-runtime-profile":
            runtime_argv = json.loads(args.argv_json)
            if not isinstance(runtime_argv, list) or not all(
                isinstance(item, str) for item in runtime_argv
            ):
                raise RuntimeError("runtime profile argv JSON must be a string list")
            print(
                json.dumps(
                    start_runtime_profile(runtime_argv, socket_path=args.socket),
                    indent=2,
                ),
                flush=True,
            )
            return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
    return 2


def _decode_response_line(raw_line: bytes) -> dict[str, Any]:
    payload = json.loads(raw_line.decode("utf-8", errors="replace"))
    if not isinstance(payload, dict):
        raise RuntimeError("PenguinBurner daemon returned an invalid response")
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
