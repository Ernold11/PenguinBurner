"""Bridge dxvk-nvapi Reflex marker trace lines into the latency receiver.

The current RE9 path uses stock Proton/DXVK-NVAPI with
``DXVK_NVAPI_LOG_LEVEL=trace``. The PenguinBurner wrapper routes stderr into an
in-memory FIFO, so dxvk-nvapi's ``NvAPI_D3D_SetLatencyMarker`` trace lines are
drained by this bridge without compiling or replacing a custom DLL.

The parser also accepts optional ``LAT`` marker-trace lines from development
builds, but the Steam setup helper does not require that custom path. The same
matcher also accepts the legacy ``PBLAT`` prefix.

This bridge tails that log, pairs SIMULATION_START (NV marker 0) with
PRESENT_END (NV marker 5) by frame id, and sends the resulting
``sim_to_present_us`` span to the PenguinBurner latency socket as a
``marker-proxy`` timing sample. When the title also emits INPUT_SAMPLE
(NV marker 6) -- the full Reflex PCL instrumentation, present in titles like
Quake II RTX but not in RE9/007 -- the bridge additionally pairs it with the
present to report ``input_to_present_us`` (the true input-to-present Reflex
lag) and ``input_to_oob_present_us`` (input through the frame-generation hold). The existing receiver -> overlay-state
publisher -> Vulkan overlay path then surfaces it as ``latency_ms`` with no
downstream changes.

When frame generation is active the application's PRESENT_END happens *before*
the generated-frame pacing displays the frame, so ``sim_to_present_us`` cannot
see the frame-generation hold (it reads unrealistically low, e.g. ~18 ms at a
40 fps base). To capture that, the bridge also pairs each base frame's
SIMULATION_START with the first OUT_OF_BAND_PRESENT_END (NV marker 12) that
occurs at or after that frame's PRESENT_END, and emits the wider
``sim_to_oob_present_us`` span. That displayed-present span is the closest
software-measurable proxy to click-to-photon: it spans real timestamps from
input/simulation up to the actual hand-off to the display, omitting only the
two unmeasurable ends (peripheral input and physical scanout/panel).

Experimental and opt-in; see docs/pc-latency-windows-tools-findings.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import stat
import time
from pathlib import Path

from .receiver import latency_socket_path, latency_socket_paths

# NV_LATENCY_MARKER_TYPE values emitted by dxvk-nvapi trace / optional tap.
NV_MARKER_SIMULATION_START = 0
NV_MARKER_PRESENT_END = 5
NV_MARKER_INPUT_SAMPLE = 6
NV_MARKER_OUT_OF_BAND_PRESENT_START = 11
NV_MARKER_OUT_OF_BAND_PRESENT_END = 12
NV_FRAMEGEN_MARKERS = {
    NV_MARKER_OUT_OF_BAND_PRESENT_START,
    NV_MARKER_OUT_OF_BAND_PRESENT_END,
}

# Optional markers-only tap: "LAT frame=<id> marker=<nv_int> t_us=<qpc_us>"
# (DXVK_NVAPI_LATENCY_MARKER_TRACE). The search also matches the legacy
# "PBLAT ..." prefix, so both forms are accepted.
_MARKER_TAP_RE = re.compile(r"LAT frame=(\d+) marker=(\d+) t_us=(\d+)")

# Stock dxvk-nvapi trace (DXVK_NVAPI_LOG_LEVEL=trace), no custom DLL needed.
# The regular Reflex path logs NvAPI_D3D_SetLatencyMarker, while async/adaptive
# frame generation can log NvAPI_D3D12_SetAsyncFrameMarker with the same
# frameID/markerType fields plus presentFrameID. Timestamp is the log line's
# seconds.milliseconds prefix (millisecond precision).
_TRACE_RE = re.compile(
    r"^(\d+)\.(\d+):.*Set(?:LatencyMarker|AsyncFrameMarker).*"
    r"frameID=(\d+),markerType=(\w+)"
)
_TRACE_MARKER_NAMES = {
    "SIMULATION_START": NV_MARKER_SIMULATION_START,
    "PRESENT_END": NV_MARKER_PRESENT_END,
    "INPUT_SAMPLE": NV_MARKER_INPUT_SAMPLE,
    "OUT_OF_BAND_PRESENT_START": NV_MARKER_OUT_OF_BAND_PRESENT_START,
    "OUT_OF_BAND_PRESENT_END": NV_MARKER_OUT_OF_BAND_PRESENT_END,
}


def _parse_line(line: str):
    """Return (frame, nv_marker, t_us) for a tap or trace line, else None."""
    m = _MARKER_TAP_RE.search(line)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    m = _TRACE_RE.match(line)
    if m:
        marker = _TRACE_MARKER_NAMES.get(m.group(4))
        if marker is None:
            return None
        t_us = (int(m.group(1)) * 1000 + int(m.group(2))) * 1000
        return int(m.group(3)), marker, t_us
    return None

# A frame id whose PRESENT_END never arrives must not leak forever; keep only a
# small window of pending SIMULATION_START timestamps.
_MAX_PENDING = 512

# An out-of-band (frame-generation) present this much later than a base frame's
# PRESENT_END is treated as a different frame's display, not this one's. Bounds a
# mis-pairing to a sane click-to-photon magnitude (log timestamps are ms-grained).
_MAX_OOB_LAG_US = 200_000


def _socket_targets(env: dict[str, str] | None = None) -> list[Path]:
    env = os.environ if env is None else env
    try:
        targets = list(latency_socket_paths(env))
    except Exception:
        targets = []
    primary = latency_socket_path(env)
    if primary not in targets:
        targets.insert(0, primary)
    return targets


def _send_sample(sock: socket.socket, targets: list[Path], sample: dict) -> None:
    line = json.dumps(sample, separators=(",", ":")).encode("utf-8")
    for target in targets:
        try:
            sock.sendto(line, str(target))
            return
        except OSError:
            continue


def _resolve_oob_present(
    sock: socket.socket,
    targets: list[Path],
    awaiting_oob: list[tuple[int, int, int, int]],
    oob_present_end_us: int,
    pid: int,
) -> int:
    """Emit the frame-generation-inclusive span for one displayed base frame.

    ``awaiting_oob`` holds (frame, input_us, sim_us, present_end_us) for base
    frames whose application present has completed but whose generated-frame
    display present has not yet been seen, in present order (``input_us`` is 0
    when the game emits no INPUT_SAMPLE marker). This out-of-band PRESENT_END
    resolves the oldest base frame whose app-present completed at or before it,
    one-to-one, so the span is not inflated by the extra generated presents.
    Returns the number of samples emitted (0 or 1).
    """
    while awaiting_oob:
        frame, input_us, sim_us, present_end_us = awaiting_oob[0]
        if present_end_us > oob_present_end_us:
            # Base frame presented after this out-of-band present; a later one
            # displays it. List is in present order, so nothing older remains.
            break
        awaiting_oob.pop(0)
        if oob_present_end_us - present_end_us > _MAX_OOB_LAG_US:
            # Too far apart to be this frame's display; drop without an OOB span.
            continue
        sample = {
            "v": 1,
            "type": "timing",
            "measurement": "marker-proxy",
            "source": "nvapi-marker-log",
            "pid": pid,
            "present_id": frame,
            "quality": "reflex-markers",
            "marker_bits": 1,
            "sim_to_present_us": present_end_us - sim_us,
            "sim_to_oob_present_us": oob_present_end_us - sim_us,
            # The displayed-present span feeds the latency ladder only. Frame
            # generation is inferred from cadence by the receiver, not asserted
            # here -- out-of-band presents occur with frame gen off too.
            "framegen_active": False,
        }
        if input_us and oob_present_end_us > input_us:
            # Game emits INPUT_SAMPLE: input -> displayed present is the widest
            # input-anchored span (full Reflex input lag through the FG hold).
            sample["input_to_present_us"] = present_end_us - input_us
            sample["input_to_oob_present_us"] = oob_present_end_us - input_us
        _send_sample(sock, targets, sample)
        return 1
    return 0


def _follow_fifo(path: Path, *, poll_interval_s: float):
    """Yield lines from a named pipe (in-memory trace stream).

    Opened read-only/non-blocking so the bridge never blocks waiting for a
    writer; lines are drained as the game produces them. On writer close
    (game exit) readline returns EOF and we reopen.
    """
    import io

    while True:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            time.sleep(poll_interval_s)
            continue
        os.set_blocking(fd, True)
        handle = io.TextIOWrapper(
            io.FileIO(fd, "r", closefd=True), encoding="utf-8", errors="replace"
        )
        try:
            while True:
                line = handle.readline()
                if line:
                    yield line
                    continue
                # Writer closed (game exited): reopen and wait for the next run.
                break
        finally:
            handle.close()
        time.sleep(poll_interval_s)


def _follow(path: Path, *, poll_interval_s: float, from_start: bool):
    """Yield lines from a FIFO (in-memory) or a growing/rotating file."""
    try:
        if path.exists() and stat.S_ISFIFO(path.stat().st_mode):
            yield from _follow_fifo(path, poll_interval_s=poll_interval_s)
            return
    except OSError:
        pass
    handle = None
    inode = None
    # Under trace logging the file grows thousands of lines/sec. If we ever
    # fall this far behind the write head, jump to live instead of slogging
    # through stale backlog -- the overlay only needs *current* latency, so a
    # gap is fine and staying current is what prevents the fallback stall.
    max_lag_bytes = 4 * 1024 * 1024
    while True:
        try:
            if handle is None:
                handle = path.open("r", encoding="utf-8", errors="replace")
                inode = os.fstat(handle.fileno()).st_ino
                if not from_start:
                    handle.seek(0, os.SEEK_END)
            line = handle.readline()
            if line:
                yield line
                continue
            # Caught up to EOF: check rotation, then skip-to-live if lagging.
            try:
                st = path.stat()
                if st.st_ino != inode:
                    handle.close()
                    handle = None
                    continue
                if st.st_size - handle.tell() > max_lag_bytes:
                    handle.seek(max(0, st.st_size - max_lag_bytes // 2))
                    handle.readline()  # discard partial line
                    continue
            except OSError:
                handle.close()
                handle = None
                continue
            time.sleep(poll_interval_s)
        except OSError:
            if handle is not None:
                handle.close()
            handle = None
            time.sleep(poll_interval_s)


def run(
    log_path: Path,
    *,
    poll_interval_s: float = 0.25,
    from_start: bool = False,
    env: dict[str, str] | None = None,
    pid: int | None = None,
) -> None:
    env = os.environ if env is None else env
    targets = _socket_targets(env)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    pid = os.getpid() if pid is None else pid

    pending_sim: dict[int, int] = {}
    pending_input: dict[int, int] = {}
    framegen_marker_frames: dict[int, int] = {}
    order: list[int] = []
    input_order: list[int] = []
    framegen_order: list[int] = []
    framegen_active_until_us = 0
    # Base frames awaiting their out-of-band (frame-generation) display present,
    # in present order: (frame, input_us, sim_us, present_end_us).
    awaiting_oob: list[tuple[int, int, int, int]] = []
    sent = 0
    for line in _follow(log_path, poll_interval_s=poll_interval_s, from_start=from_start):
        parsed = _parse_line(line)
        if parsed is None:
            continue
        frame, marker, t_us = parsed
        if marker in NV_FRAMEGEN_MARKERS:
            if frame not in framegen_marker_frames:
                framegen_order.append(frame)
            framegen_marker_frames[frame] = t_us
            framegen_active_until_us = max(framegen_active_until_us, t_us + 3_000_000)
            while len(framegen_order) > _MAX_PENDING:
                framegen_marker_frames.pop(framegen_order.pop(0), None)
            if marker == NV_MARKER_OUT_OF_BAND_PRESENT_END:
                sent += _resolve_oob_present(
                    sock, targets, awaiting_oob, t_us, pid
                )
            continue
        if marker == NV_MARKER_INPUT_SAMPLE:
            # Emitted by titles with full Reflex PCL markers (e.g. Quake II RTX),
            # right before SIMULATION_START. Anchors the true input-to-present lag.
            if frame not in pending_input:
                input_order.append(frame)
            pending_input[frame] = t_us
            while len(input_order) > _MAX_PENDING:
                pending_input.pop(input_order.pop(0), None)
        elif marker == NV_MARKER_SIMULATION_START:
            if frame not in pending_sim:
                order.append(frame)
            pending_sim[frame] = t_us
            while len(order) > _MAX_PENDING:
                pending_sim.pop(order.pop(0), None)
        elif marker == NV_MARKER_PRESENT_END:
            sim_us = pending_sim.pop(frame, None)
            if sim_us is None or t_us <= sim_us:
                pending_input.pop(frame, None)
                continue
            input_us = pending_input.pop(frame, 0)
            span_us = t_us - sim_us
            framegen_marker_us = framegen_marker_frames.pop(frame, 0)
            # Recent out-of-band present activity means a displayed-present marker
            # is expected for this frame. It is NOT evidence of frame generation:
            # Reflex emits out-of-band presents with frame gen off, so the bridge
            # never asserts framegen_active. The receiver decides frame generation
            # from the displayed-vs-base cadence ratio instead.
            oob_present_recent = (
                bool(framegen_marker_us) or t_us <= framegen_active_until_us
            )
            sample = {
                "v": 1,
                "type": "timing",
                "measurement": "marker-proxy",
                "source": "nvapi-marker-log",
                "pid": pid,
                "present_id": frame,
                "quality": "reflex-markers",
                "marker_bits": 1,
                "sim_to_present_us": span_us,
                "framegen_active": False,
            }
            if input_us and t_us > input_us:
                # Full Reflex input lag: input sample -> application present.
                sample["input_to_present_us"] = t_us - input_us
            _send_sample(sock, targets, sample)
            sent += 1
            if oob_present_recent:
                # Wait for the displayed (out-of-band) present to emit the wider
                # sim_to_oob_present_us span for the click-to-photon proxy.
                awaiting_oob.append((frame, input_us, sim_us, t_us))
                while len(awaiting_oob) > _MAX_PENDING:
                    awaiting_oob.pop(0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Bridge dxvk-nvapi trace marker records into the latency socket."
        )
    )
    default_log = Path.home() / "steam-3764200.log"
    parser.add_argument("--log", type=Path, default=default_log)
    parser.add_argument("--poll-interval", type=float, default=0.25)
    parser.add_argument(
        "--from-start",
        action="store_true",
        help="Process the whole log instead of only new lines.",
    )
    args = parser.parse_args(argv)
    run(args.log, poll_interval_s=args.poll_interval, from_start=args.from_start)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
