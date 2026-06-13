from __future__ import annotations

from latency_telemetry.nvapi_marker_bridge import (
    NV_MARKER_INPUT_SAMPLE,
    NV_MARKER_OUT_OF_BAND_PRESENT_END,
    NV_MARKER_PRESENT_END,
    NV_MARKER_SIMULATION_START,
    _parse_line,
    run,
)


def test_parse_line_accepts_input_sample_marker() -> None:
    assert _parse_line(
        "123.456:trace:nvapi64:NvAPI_D3D_SetLatencyMarker "
        "({version=1,frameID=42,markerType=INPUT_SAMPLE,rsvd})"
    ) == (42, NV_MARKER_INPUT_SAMPLE, 123456000)


def test_parse_line_accepts_stock_dxvk_nvapi_trace_marker() -> None:
    assert _parse_line(
        "123.456:trace:nvapi64:NvAPI_D3D_SetLatencyMarker "
        "({version=1,frameID=42,markerType=SIMULATION_START,rsvd})"
    ) == (42, NV_MARKER_SIMULATION_START, 123456000)


def test_parse_line_accepts_lat_marker_tap() -> None:
    assert _parse_line("LAT frame=42 marker=5 t_us=123456789") == (
        42,
        NV_MARKER_PRESENT_END,
        123456789,
    )


def test_parse_line_accepts_legacy_pblat_marker() -> None:
    assert _parse_line("PBLAT frame=42 marker=5 t_us=123456789") == (
        42,
        NV_MARKER_PRESENT_END,
        123456789,
    )


def test_parse_line_accepts_stock_dxvk_nvapi_oob_present_marker() -> None:
    assert _parse_line(
        "123.456:trace:nvapi64:NvAPI_D3D_SetLatencyMarker "
        "({version=1,frameID=42,markerType=OUT_OF_BAND_PRESENT_END,rsvd})"
    ) == (42, NV_MARKER_OUT_OF_BAND_PRESENT_END, 123456000)


def test_parse_line_accepts_stock_dxvk_nvapi_async_frame_marker() -> None:
    assert _parse_line(
        "123.456:trace:nvapi64:NvAPI_D3D12_SetAsyncFrameMarker "
        "({version=1,frameID=42,markerType=OUT_OF_BAND_PRESENT_END,"
        "presentFrameID=77,rsvd})"
    ) == (42, NV_MARKER_OUT_OF_BAND_PRESENT_END, 123456000)


def test_parse_line_accepts_oob_present_marker_tap() -> None:
    assert _parse_line("LAT frame=42 marker=12 t_us=123456789") == (
        42,
        NV_MARKER_OUT_OF_BAND_PRESENT_END,
        123456789,
    )


def test_bridge_does_not_mark_framegen_from_oob_present_trace(monkeypatch, tmp_path) -> None:
    # Reflex out-of-band present markers are emitted with frame generation OFF, so
    # the bridge must never assert frame generation from them -- it emits the span
    # only and leaves the on/off decision to the receiver's cadence check.
    import latency_telemetry.nvapi_marker_bridge as bridge

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
        lambda _path, poll_interval_s, from_start: lines,
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
    import latency_telemetry.nvapi_marker_bridge as bridge

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
        lambda _path, poll_interval_s, from_start: lines,
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
    import latency_telemetry.nvapi_marker_bridge as bridge

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
        lambda _path, poll_interval_s, from_start: lines,
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
