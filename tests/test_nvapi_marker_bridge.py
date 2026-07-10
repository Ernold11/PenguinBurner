from __future__ import annotations

from overlay.telemetry.nvapi_marker_bridge import (
    NV_MARKER_INPUT_SAMPLE,
    NV_MARKER_OUT_OF_BAND_PRESENT_END,
    NV_MARKER_SIMULATION_START,
    _parse_line_with_pid,
    run,
)


def test_parse_line_accepts_input_sample_marker() -> None:
    assert _parse_line_with_pid(
        "123.456:trace:nvapi64:NvAPI_D3D_SetLatencyMarker "
        "({version=1,frameID=42,markerType=INPUT_SAMPLE,rsvd})"
    ) == (42, NV_MARKER_INPUT_SAMPLE, 123456000, None)


def test_parse_line_accepts_stock_dxvk_nvapi_trace_marker() -> None:
    assert _parse_line_with_pid(
        "123.456:trace:nvapi64:NvAPI_D3D_SetLatencyMarker "
        "({version=1,frameID=42,markerType=SIMULATION_START,rsvd})"
    ) == (42, NV_MARKER_SIMULATION_START, 123456000, None)


def test_parse_line_accepts_stock_dxvk_nvapi_oob_present_marker() -> None:
    assert _parse_line_with_pid(
        "123.456:trace:nvapi64:NvAPI_D3D_SetLatencyMarker "
        "({version=1,frameID=42,markerType=OUT_OF_BAND_PRESENT_END,rsvd})"
    ) == (42, NV_MARKER_OUT_OF_BAND_PRESENT_END, 123456000, None)


def test_parse_line_accepts_stock_dxvk_nvapi_async_frame_marker() -> None:
    assert _parse_line_with_pid(
        "123.456:trace:nvapi64:NvAPI_D3D12_SetAsyncFrameMarker "
        "({version=1,frameID=42,markerType=OUT_OF_BAND_PRESENT_END,"
        "presentFrameID=77,rsvd})"
    ) == (42, NV_MARKER_OUT_OF_BAND_PRESENT_END, 123456000, None)


def test_parse_line_accepts_dxvk_nvapi_marker_only_log() -> None:
    assert _parse_line_with_pid(
        "123.456:1abc:2def:latency-marker:nvapi64:"
        "qpcUs=987654321 api=d3d frameID=42 markerType=SIMULATION_START "
        "markerValue=0"
    ) == (42, NV_MARKER_SIMULATION_START, 987654321, 0x1ABC)


def test_parse_line_accepts_dxvk_nvapi_marker_only_async_log() -> None:
    assert _parse_line_with_pid(
        "123.456:1abc:2def:latency-marker:nvapi64:"
        "qpcUs=987654321 api=d3d12_async frameID=42 "
        "markerType=OUT_OF_BAND_PRESENT_END markerValue=12 presentFrameID=77"
    ) == (42, NV_MARKER_OUT_OF_BAND_PRESENT_END, 987654321, 0x1ABC)


def test_bridge_uses_marker_only_log_process_id(monkeypatch, tmp_path) -> None:
    import overlay.telemetry.nvapi_marker_bridge as bridge

    lines = iter(
        (
            "123.456:0000002a:2def:latency-marker:nvapi64:"
            "qpcUs=1000000 api=d3d frameID=7 markerType=SIMULATION_START "
            "markerValue=0",
            "123.466:0000002a:2def:latency-marker:nvapi64:"
            "qpcUs=1010000 api=d3d frameID=7 markerType=PRESENT_END "
            "markerValue=5",
        )
    )
    samples = []

    monkeypatch.setattr(
        bridge,
        "_follow",
        lambda _path, poll_interval_s, from_start, stop_event=None,
        session_alive_fn=None, session_quiesced_fn=None: lines,
    )
    monkeypatch.setattr(
        bridge,
        "_send_sample",
        lambda _sock, _targets, sample: samples.append(sample),
    )

    run(tmp_path / "trace.fifo", env={}, pid=999)

    assert len(samples) == 1
    assert samples[0]["pid"] == 42
    assert samples[0]["sim_to_present_us"] == 10000


def test_bridge_does_not_mark_framegen_from_oob_present_trace(monkeypatch, tmp_path) -> None:
    # Reflex out-of-band present markers are emitted with frame generation OFF, so
    # the bridge must never assert frame generation from them -- it emits the span
    # only and leaves the on/off decision to the receiver's cadence check.
    import overlay.telemetry.nvapi_marker_bridge as bridge

    lines = iter(
        (
            "1.000:trace:nvapi64:NvAPI_D3D_SetLatencyMarker "
            "({version=1,frameID=7,markerType=SIMULATION_START,rsvd})",
            "1.005:trace:nvapi64:NvAPI_D3D_SetLatencyMarker "
            "({version=1,frameID=7,markerType=OUT_OF_BAND_PRESENT_END,rsvd})",
            "1.010:trace:nvapi64:NvAPI_D3D_SetLatencyMarker "
            "({version=1,frameID=7,markerType=PRESENT_END,rsvd})",
        )
    )
    samples = []

    monkeypatch.setattr(
        bridge,
        "_follow",
        lambda _path, poll_interval_s, from_start, stop_event=None,
        session_alive_fn=None, session_quiesced_fn=None: lines,
    )
    monkeypatch.setattr(
        bridge,
        "_send_sample",
        lambda _sock, _targets, sample: samples.append(sample),
    )

    run(tmp_path / "trace.fifo", env={}, pid=123)

    assert len(samples) == 1
    assert samples[0]["sim_to_present_us"] == 10000
    assert samples[0]["framegen_active"] is False
    assert "framegen_frame_count" not in samples[0]


def test_bridge_emits_oob_present_span_in_present_order(monkeypatch, tmp_path) -> None:
    # Realistic frame-gen order: the application PRESENT_END precedes the
    # out-of-band display present that shows the frame. A prior out-of-band present
    # primes the expectation so the base frame is paired with its later display
    # present, yielding the wider sim_to_oob_present_us span (still not flagged as
    # frame generation -- the receiver decides that from cadence).
    import overlay.telemetry.nvapi_marker_bridge as bridge

    lines = iter(
        (
            "0.950:trace:nvapi64:NvAPI_D3D_SetLatencyMarker "
            "({version=1,frameID=6,markerType=OUT_OF_BAND_PRESENT_END,rsvd})",
            "1.000:trace:nvapi64:NvAPI_D3D_SetLatencyMarker "
            "({version=1,frameID=7,markerType=SIMULATION_START,rsvd})",
            "1.010:trace:nvapi64:NvAPI_D3D_SetLatencyMarker "
            "({version=1,frameID=7,markerType=PRESENT_END,rsvd})",
            "1.060:trace:nvapi64:NvAPI_D3D_SetLatencyMarker "
            "({version=1,frameID=7,markerType=OUT_OF_BAND_PRESENT_END,rsvd})",
        )
    )
    samples = []

    monkeypatch.setattr(
        bridge,
        "_follow",
        lambda _path, poll_interval_s, from_start, stop_event=None,
        session_alive_fn=None, session_quiesced_fn=None: lines,
    )
    monkeypatch.setattr(
        bridge,
        "_send_sample",
        lambda _sock, _targets, sample: samples.append(sample),
    )

    run(tmp_path / "trace.fifo", env={}, pid=123)

    oob_samples = [s for s in samples if "sim_to_oob_present_us" in s]
    assert len(oob_samples) == 1
    assert oob_samples[0]["present_id"] == 7
    assert oob_samples[0]["sim_to_oob_present_us"] == 60000
    assert oob_samples[0]["sim_to_present_us"] == 10000
    assert oob_samples[0]["framegen_active"] is False


def test_bridge_reports_input_to_present_when_input_marker_present(monkeypatch, tmp_path) -> None:
    # Title with full Reflex PCL markers (e.g. Quake II RTX): INPUT_SAMPLE pairs
    # with PRESENT_END to give the true input-to-present Reflex lag.
    import overlay.telemetry.nvapi_marker_bridge as bridge

    lines = iter(
        (
            "1.000:trace:nvapi64:NvAPI_D3D_SetLatencyMarker "
            "({version=1,frameID=7,markerType=INPUT_SAMPLE,rsvd})",
            "1.002:trace:nvapi64:NvAPI_D3D_SetLatencyMarker "
            "({version=1,frameID=7,markerType=SIMULATION_START,rsvd})",
            "1.030:trace:nvapi64:NvAPI_D3D_SetLatencyMarker "
            "({version=1,frameID=7,markerType=PRESENT_END,rsvd})",
        )
    )
    samples = []

    monkeypatch.setattr(
        bridge,
        "_follow",
        lambda _path, poll_interval_s, from_start, stop_event=None,
        session_alive_fn=None, session_quiesced_fn=None: lines,
    )
    monkeypatch.setattr(
        bridge,
        "_send_sample",
        lambda _sock, _targets, sample: samples.append(sample),
    )

    run(tmp_path / "trace.fifo", env={}, pid=123)

    assert len(samples) == 1
    # input -> present (30ms) is wider than sim -> present (28ms): the input lag.
    assert samples[0]["input_to_present_us"] == 30000
    assert samples[0]["sim_to_present_us"] == 28000


def test_drainer_exits_when_session_dead_and_no_writers(tmp_path, monkeypatch) -> None:
    """Detached-drainer lifetime: the launching session died before any writer
    ever connected (e.g. failed exec); after the no-writer grace, run() returns
    instead of tailing a pipe that can never fill."""
    import os
    import overlay.telemetry.nvapi_marker_bridge as bridge

    monkeypatch.setattr(bridge, "_NO_WRITER_GRACE_S", 0.05)
    fifo = tmp_path / "nvapi-trace.1.fifo"
    os.mkfifo(fifo)

    bridge.run(fifo, poll_interval_s=0.01, session_alive_fn=lambda: False)
    # Returned promptly (a hang here would fail the test by timeout).


def test_drainer_keeps_draining_while_writer_exists(tmp_path) -> None:
    """A dead session does not stop the drain while any writer holds the FIFO
    (a wrapped game can outlive the wrapper session that launched it)."""
    import os
    import threading
    import time
    import overlay.telemetry.nvapi_marker_bridge as bridge

    fifo = tmp_path / "nvapi-trace.1.fifo"
    os.mkfifo(fifo)
    samples = []

    def send(_sock, _targets, sample):
        samples.append(sample)

    thread = threading.Thread(
        target=lambda: bridge.run(
            fifo,
            poll_interval_s=0.01,
            session_alive_fn=lambda: False,  # session died immediately
            env={},
        ),
        daemon=True,
    )
    import unittest.mock

    with unittest.mock.patch.object(bridge, "_send_sample", send):
        writer = os.open(fifo, os.O_RDWR)  # the surviving game's stderr fd
        thread.start()
        os.write(
            writer,
            b"123.456:2a:0:latency-marker:pb:qpcUs=1000000 frameID=7 "
            b"markerType=SIMULATION_START\n"
            b"123.466:2a:0:latency-marker:pb:qpcUs=1010000 frameID=7 "
            b"markerType=PRESENT_END\n",
        )
        deadline = time.monotonic() + 2.0
        while not samples and time.monotonic() < deadline:
            time.sleep(0.01)
        assert samples, "markers must be drained while the game lives"
        assert samples[0]["sim_to_present_us"] == 10000
        os.close(writer)  # last writer gone -> EOF -> session check -> exit
    thread.join(timeout=2.0)
    assert not thread.is_alive()


def test_drainer_exits_when_steam_reaper_quiesces_after_traffic(tmp_path) -> None:
    """Steam's reaper keeps the FIFO write side open after the game is gone; the
    drainer must still exit once the reaper has only PB helper children left."""
    import os
    import threading
    import time
    import overlay.telemetry.nvapi_marker_bridge as bridge

    fifo = tmp_path / "nvapi-trace.1.fifo"
    os.mkfifo(fifo)

    quiesced = threading.Event()
    thread = threading.Thread(
        target=lambda: bridge.run(
            fifo,
            poll_interval_s=0.01,
            session_alive_fn=lambda: True,
            session_quiesced_fn=lambda: quiesced.is_set(),
            env={},
        ),
        daemon=True,
    )
    writer = os.open(fifo, os.O_RDWR)  # Steam reaper's inherited stderr fd
    try:
        thread.start()
        os.write(
            writer,
            b"123.456:2a:0:latency-marker:pb:qpcUs=1000000 frameID=7 "
            b"markerType=SIMULATION_START\n",
        )
        time.sleep(0.05)
        quiesced.set()
        thread.join(timeout=2.0)
        assert not thread.is_alive()
    finally:
        os.close(writer)


def test_drainer_main_cleans_up_fifo(tmp_path, monkeypatch) -> None:
    """--cleanup unlinks the per-launch FIFO once the watch ends."""
    import os
    import overlay.telemetry.nvapi_marker_bridge as bridge

    monkeypatch.setattr(bridge, "_NO_WRITER_GRACE_S", 0.05)
    fifo = tmp_path / "nvapi-trace.9.fifo"
    os.mkfifo(fifo)
    # A pid that cannot exist: the session is dead from the first check, and
    # no writer ever connects, so the watch ends after the grace.
    rc = bridge.main(
        ["--log", str(fifo), "--session-pid", "2147483646", "--cleanup",
         "--poll-interval", "0.01"]
    )
    assert rc == 0
    assert not fifo.exists()


def test_spawn_detached_drainer_argv(monkeypatch, tmp_path) -> None:
    import sys
    import overlay.telemetry.nvapi_marker_bridge as bridge

    captured = {}

    class FakePopen:
        def __init__(self, argv, **kwargs) -> None:
            captured["argv"] = argv
            captured["kwargs"] = kwargs

    monkeypatch.setattr(bridge.subprocess, "Popen", FakePopen)
    fifo = tmp_path / "nvapi-trace.5.fifo"
    env = {"HOME": str(tmp_path)}

    assert bridge.spawn_detached_drainer(env, fifo, session_pid=41) is not None
    assert captured["argv"] == [
        sys.executable,
        "-m",
        "overlay.telemetry.nvapi_marker_bridge",
        "--log",
        str(fifo),
        "--cleanup",
        "--session-pid=41",
    ]
    assert captured["kwargs"]["start_new_session"] is True
    assert captured["kwargs"]["env"] is env
