from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import socket
import sys
from typing import Any

from runtime.daemon_api import DEFAULT_DAEMON_SOCKET


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


def start_game_runtime_profile(
    argv: list[str],
    *,
    watch_pid: int,
    app_id: str = "",
    socket_path: str | Path = DEFAULT_DAEMON_SOCKET,
    timeout_s: float = 3.0,
) -> dict[str, Any]:
    return daemon_payload_request(
        {
            "method": "start_game_runtime_profile",
            "argv": list(argv),
            "watch_pid": int(watch_pid),
            "app_id": str(app_id),
        },
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
        default=DEFAULT_DAEMON_SOCKET,
        help=argparse.SUPPRESS,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    subparsers.add_parser("stop-auto-uv")
    subparsers.add_parser("stop-runtime-profile")
    start = subparsers.add_parser("start-auto-uv")
    start.add_argument("options_json")
    runtime = subparsers.add_parser("start-runtime-profile")
    runtime.add_argument("argv_json")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

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
        if args.command == "start-auto-uv":
            options = json.loads(args.options_json)
            if not isinstance(options, dict):
                raise RuntimeError("Auto-UV options JSON must be an object")
            return stream_auto_uv_scan(options, socket_path=args.socket)
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
