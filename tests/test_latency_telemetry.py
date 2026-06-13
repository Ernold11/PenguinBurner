import json
from pathlib import Path

import latency_telemetry.nvapi_marker_bridge as marker_bridge
import latency_telemetry.receiver as receiver
from latency_telemetry.receiver import (
    LatencyTelemetryLogger,
    LatencyTelemetryMeter,
    latency_socket_path,
    latency_socket_paths,
)


def test_latency_telemetry_meter_reports_present_pacing() -> None:
    now = 100.0
    meter = LatencyTelemetryMeter(time_monotonic=lambda: now)
    for frametime_us in (16600, 16700, 16600, 50000):
        meter.add_sample(
            {
                "type": "timing",
                "measurement": "present-pacing",
                "pid": 123,
                "quality": "present-frametime",
                "present_frametime_us": frametime_us,
            }
        )

    summary = meter.summary(now=101.25)

    assert summary is not None
    assert summary == (
        "event=latency-meter pid=123 quality=present-frametime samples=4 "
        "present-frametime-p95=50.00ms present-fps=20 "
        "raw-present-fps-avg=40 raw-present-fps-median=60 "
        "raw-present-fps-5pct-low=20 raw-present-fps-1pct-low=20"
    )
    assert "render-present-p95" not in summary
    assert "gpu-render-p95" not in summary
    assert "input-present-p95" not in summary
    assert "gpu-frame-p95" not in summary


def test_latency_telemetry_logger_claims_socket_ownership(tmp_path, monkeypatch) -> None:
    calls = []
    socket_path = tmp_path / "runtime" / "latency.sock"
    monkeypatch.setattr(
        receiver,
        "claim_desktop_user_ownership",
        lambda path, **kwargs: calls.append((Path(path), dict(kwargs))),
    )

    logger = LatencyTelemetryLogger(paths=[socket_path], log=lambda message: None).start()
    logger.close()

    assert (socket_path.parent, {"include_parents": True}) in calls
    assert (socket_path, {}) in calls


def test_latency_telemetry_meter_reports_present_fps_stats_over_sample_window() -> None:
    clock = {"now": 99.0}
    meter = LatencyTelemetryMeter(
        max_sample_age_s=3.0,
        time_monotonic=lambda: clock["now"],
    )
    for sample_time, frametime_us in (
        (99.0, 100000),
        (100.0, 16667),
        (101.0, 16667),
        (102.0, 33333),
        (103.0, 50000),
    ):
        clock["now"] = sample_time
        meter.add_sample(
            {
                "type": "timing",
                "measurement": "present-pacing",
                "pid": 123,
                "quality": "present-frametime",
                "present_frametime_us": frametime_us,
            }
        )

    summary = meter.summary(now=103.0)

    assert summary is not None
    assert "present-frametime-p95=50.00ms" in summary
    assert "present-fps=20" in summary
    assert "raw-present-fps-median=40" in summary
    assert "raw-present-fps-avg=34" in summary
    assert "raw-present-fps-5pct-low=20" in summary
    assert "raw-present-fps-1pct-low=20" in summary


def test_latency_telemetry_meter_keeps_full_three_second_high_fps_window() -> None:
    clock = {"now": 100.0}
    meter = LatencyTelemetryMeter(
        max_sample_age_s=3.0,
        time_monotonic=lambda: clock["now"],
    )

    for index in range(360):
        clock["now"] = 100.0 + index / 120.0
        meter.add_sample(
            {
                "type": "timing",
                "measurement": "present-pacing",
                "pid": 123,
                "quality": "present-frametime",
                "present_frametime_us": 8333,
            }
        )

    summary = meter.summary(now=103.0)

    assert summary is not None
    assert "samples=360" in summary
    assert "present-frametime-p95=8.33ms" in summary
    assert "present-fps=n/a" in summary
    assert "raw-present-fps-avg=120" in summary


def test_latency_telemetry_meter_suppresses_single_sample_restart_spike() -> None:
    meter = LatencyTelemetryMeter(time_monotonic=lambda: 100.0)
    meter.add_sample(
        {
            "type": "timing",
            "measurement": "present-pacing",
            "pid": 123,
            "quality": "present-frametime",
            "present_frametime_us": 7900,
        }
    )

    summary = meter.summary(now=100.0)

    assert summary is not None
    assert "samples=1" in summary
    assert "present-frametime-p95=n/a" in summary
    assert "present-fps=n/a" in summary
    assert "raw-present-fps-avg=n/a" in summary


def test_latency_telemetry_meter_deinterlaces_output_cadence_from_previous_base() -> None:
    clock = {"now": 100.0}
    meter = LatencyTelemetryMeter(
        max_sample_age_s=3.0,
        time_monotonic=lambda: clock["now"],
    )

    for index in range(120):
        clock["now"] = 100.0 + index / 40.0
        meter.add_sample(
            {
                "type": "timing",
                "measurement": "present-pacing",
                "pid": 123,
                "quality": "present-frametime",
                "present_frametime_us": 25000,
            }
        )

    base_summary = meter.summary(now=103.0)
    assert base_summary is not None
    assert "present-fps=40" in base_summary

    for index in range(360):
        clock["now"] = 104.0 + index / 120.0
        meter.add_sample(
            {
                "type": "timing",
                "measurement": "present-pacing",
                "pid": 123,
                "quality": "present-frametime",
                "present_frametime_us": 8333,
            }
        )

    output_summary = meter.summary(now=107.0)

    assert output_summary is not None
    assert "present-frametime-p95=8.33ms" in output_summary
    assert "present-fps=40" in output_summary
    assert "raw-present-fps-avg=120" in output_summary


def test_latency_telemetry_meter_prefers_base_frame_markers_over_output_presents() -> None:
    clock = {"now": 100.0}
    meter = LatencyTelemetryMeter(
        max_sample_age_s=3.0,
        time_monotonic=lambda: clock["now"],
    )

    for index in range(360):
        clock["now"] = 100.0 + index / 120.0
        meter.add_sample(
            {
                "type": "timing",
                "measurement": "present-pacing",
                "pid": 123,
                "quality": "present-frametime",
                "present_frametime_us": 8333,
            }
        )

    for index in range(120):
        clock["now"] = 100.0 + index / 40.0
        meter.add_sample(
            {
                "type": "timing",
                "measurement": "base-frame-marker-pacing",
                "pid": 123,
                "quality": "base-frame-marker",
                "base_frame_id": index + 1,
                "base_frame_frametime_us": 25000,
            }
        )

    summary = meter.summary(now=103.0)

    assert summary is not None
    assert "quality=base-frame-marker" in summary
    assert "present-fps=40" in summary
    assert "raw-present-fps-avg=120" in summary


def test_latency_telemetry_snapshot_exposes_base_present_cadence() -> None:
    clock = {"now": 100.0}
    meter = LatencyTelemetryMeter(
        max_sample_age_s=3.0,
        time_monotonic=lambda: clock["now"],
    )

    for index in range(360):
        clock["now"] = 100.0 + index / 120.0
        meter.add_sample(
            {
                "type": "timing",
                "measurement": "present-pacing",
                "pid": 123,
                "quality": "present-frametime",
                "present_frametime_us": 8333,
            }
        )

    for index in range(120):
        clock["now"] = 100.0 + index / 40.0
        meter.add_sample(
            {
                "type": "timing",
                "measurement": "base-frame-marker-pacing",
                "pid": 123,
                "quality": "base-frame-marker",
                "base_frame_id": index + 1,
                "base_frame_frametime_us": 25000,
                "marker_name": "present-start",
            }
        )

    snapshot = meter.snapshot(now=103.0)

    assert snapshot is not None
    assert snapshot["present_frametime_p95_ms"] == 8.333
    assert snapshot["present_fps"] == "40"
    assert snapshot["base_present_fps"] == 40
    assert snapshot["base_present_frametime_p95_ms"] == 25.0
    assert snapshot["raw_present_fps_stats"]["avg"] == "120"
    # Display cadence (120) is 3x the base render cadence (40): frames are being
    # generated, so the overlay reports frame generation from the cadence ratio.
    assert snapshot["framegen_active"] is True


def test_latency_telemetry_snapshot_marks_framegen_only_from_explicit_evidence() -> None:
    meter = LatencyTelemetryMeter(time_monotonic=lambda: 100.0)
    meter.add_sample(
        {
            "type": "timing",
            "measurement": "present-pacing",
            "pid": 123,
            "quality": "present-frametime",
            "present_frametime_us": 8333,
            "framegen_active": True,
        }
    )

    snapshot = meter.snapshot(now=100.5)

    assert snapshot is not None
    assert snapshot["framegen_active"] is True


def test_latency_telemetry_snapshot_marks_framegen_from_generated_frame_count() -> None:
    meter = LatencyTelemetryMeter(time_monotonic=lambda: 100.0)
    meter.add_sample(
        {
            "type": "timing",
            "measurement": "generated-frame",
            "pid": 123,
            "quality": "present-frametime",
            "generated_frame_count": 1,
        }
    )

    snapshot = meter.snapshot(now=100.5)

    assert snapshot is not None
    assert snapshot["framegen_active"] is True


def test_latency_telemetry_snapshot_ignores_oob_present_marker_without_cadence() -> None:
    # Reflex out-of-band present markers are emitted with frame generation off, so
    # an oob-present marker alone -- without a multiplied display cadence -- must
    # NOT report frame generation (this is the phantom "FG" rate fix).
    meter = LatencyTelemetryMeter(time_monotonic=lambda: 100.0)
    meter.add_sample(
        {
            "type": "timing",
            "measurement": "base-frame-marker-pacing",
            "pid": 123,
            "quality": "base-frame-marker",
            "base_frame_id": 1,
            "base_frame_frametime_us": 25000,
            "marker_name": "oob-present-start",
        }
    )

    snapshot = meter.snapshot(now=100.5)

    assert snapshot is not None
    assert snapshot["framegen_active"] is False


def test_latency_telemetry_snapshot_no_framegen_when_oob_span_but_base_cadence() -> None:
    # Regression: frame generation turned OFF in-game, but Reflex still emits
    # out-of-band present markers (so a sim->displayed-present span exists). With
    # the display cadence == base cadence, the overlay must NOT show a frame-gen
    # rate, yet the latency proxy still uses the wider displayed-present span.
    clock = {"now": 100.0}
    meter = LatencyTelemetryMeter(
        max_sample_age_s=3.0,
        time_monotonic=lambda: clock["now"],
    )
    for index in range(120):
        clock["now"] = 100.0 + index / 40.0
        meter.add_sample(
            {
                "type": "timing",
                "measurement": "present-pacing",
                "pid": 123,
                "quality": "present-frametime",
                "present_frametime_us": 25000,
            }
        )
        meter.add_sample(
            {
                "type": "timing",
                "measurement": "base-frame-marker-pacing",
                "pid": 123,
                "quality": "base-frame-marker",
                "base_frame_id": index + 1,
                "base_frame_frametime_us": 25000,
                "marker_name": "present-start",
            }
        )
    meter.add_sample(
        _marker_proxy_sample(sim_to_present_us=40000, sim_to_oob_present_us=60000)
    )

    snapshot = meter.snapshot(now=103.0)

    assert snapshot is not None
    assert snapshot["base_present_fps"] == 40
    assert snapshot["framegen_active"] is False
    assert snapshot["latency_quality"] == "sim-to-oob-present"
    assert snapshot["latency_proxy_p95_us"] == 60000


def test_latency_telemetry_meter_prefers_oob_marker_source_over_fast_marker_source() -> None:
    clock = {"now": 100.0}
    meter = LatencyTelemetryMeter(
        max_sample_age_s=3.0,
        time_monotonic=lambda: clock["now"],
    )

    for index in range(360):
        clock["now"] = 100.0 + index / 120.0
        meter.add_sample(
            {
                "type": "timing",
                "measurement": "present-pacing",
                "pid": 123,
                "quality": "present-frametime",
                "present_frametime_us": 8333,
            }
        )

    for index in range(360):
        clock["now"] = 100.0 + index / 120.0
        meter.add_sample(
            {
                "type": "timing",
                "measurement": "base-frame-marker-pacing",
                "pid": 123,
                "quality": "base-frame-marker",
                "base_frame_id": index + 1,
                "base_frame_frametime_us": 8333,
                "marker_name": "rendersubmit-start",
            }
        )

    for index in range(120):
        clock["now"] = 100.0 + index / 40.0
        meter.add_sample(
            {
                "type": "timing",
                "measurement": "base-frame-marker-pacing",
                "pid": 123,
                "quality": "base-frame-marker",
                "base_frame_id": index + 1,
                "base_frame_frametime_us": 25000,
                "marker_name": "oob-present-start",
            }
        )

    summary = meter.summary(now=103.0)

    assert summary is not None
    assert "quality=base-frame-marker" in summary
    assert "present-fps=40" in summary
    assert "raw-present-fps-avg=120" in summary


def test_latency_telemetry_meter_falls_back_to_present_start_marker_source() -> None:
    clock = {"now": 100.0}
    meter = LatencyTelemetryMeter(
        max_sample_age_s=3.0,
        time_monotonic=lambda: clock["now"],
    )

    for index in range(120):
        clock["now"] = 100.0 + index / 40.0
        meter.add_sample(
            {
                "type": "timing",
                "measurement": "base-frame-marker-pacing",
                "pid": 123,
                "quality": "base-frame-marker",
                "base_frame_id": index + 1,
                "base_frame_frametime_us": 25000,
                "marker_name": "present-start",
            }
        )

    summary = meter.summary(now=103.0)

    assert summary is not None
    assert "quality=base-frame-marker" in summary
    assert "present-fps=40" in summary


def test_latency_telemetry_meter_rejects_marker_source_faster_than_output() -> None:
    clock = {"now": 100.0}
    meter = LatencyTelemetryMeter(
        max_sample_age_s=3.0,
        time_monotonic=lambda: clock["now"],
    )

    for index in range(300):
        clock["now"] = 100.0 + index / 100.0
        meter.add_sample(
            {
                "type": "timing",
                "measurement": "present-pacing",
                "pid": 123,
                "quality": "present-frametime",
                "present_frametime_us": 10000,
            }
        )

    for index in range(300):
        clock["now"] = 100.0 + index / 100.0
        meter.add_sample(
            {
                "type": "timing",
                "measurement": "base-frame-marker-pacing",
                "pid": 123,
                "quality": "base-frame-marker",
                "base_frame_id": index + 1,
                "base_frame_frametime_us": 5000,
                "marker_name": "oob-present-start",
            }
        )

    summary = meter.summary(now=103.0)

    assert summary is not None
    assert "quality=base-frame-marker" in summary
    assert "present-frametime-p95=10.00ms" in summary
    assert "present-fps=n/a" in summary
    assert "raw-present-fps-avg=100" in summary


def test_latency_telemetry_meter_ignores_non_timing_samples() -> None:
    meter = LatencyTelemetryMeter(time_monotonic=lambda: 100.0)

    assert meter.add_sample({"type": "status", "event": "ignored"}) is None
    assert meter.summary(now=100.0) is None


def test_latency_telemetry_logger_formats_raw_present_timing_events() -> None:
    logs: list[str] = []
    logger = LatencyTelemetryLogger(
        log=logs.append,
        raw_log_interval_s=0.0,
        time_monotonic=lambda: 42.0,
        time_strftime=lambda _format: "2026-06-03 22:00:00",
    )

    logger._maybe_log_raw_timing(
        {
            "type": "timing",
            "measurement": "present-pacing",
            "pid": 123,
            "device": "0x1",
            "swapchain": "0x2",
            "quality": "present-frametime",
            "present_count": 99,
            "present_frametime_us": 16667,
        }
    )

    assert logs == [
        "2026-06-03 22:00:00 event=latency-raw pid=123 "
        "measurement=present-pacing device=0x1 swapchain=0x2 "
        "quality=present-frametime present_count=99 present_frametime_us=16667"
    ]


def test_latency_telemetry_raw_logs_are_off_by_default() -> None:
    assert receiver._raw_timing_log_interval({}) is None


def test_latency_socket_path_uses_sudo_user_runtime_for_root_service(
    monkeypatch, tmp_path
) -> None:
    run_user = tmp_path / "run" / "user" / "1000"
    run_user.mkdir(parents=True)
    real_path = receiver.Path

    def fake_path(*parts):
        if parts == ("/run/user",):
            return tmp_path / "run" / "user"
        return real_path(*parts)

    monkeypatch.setattr(receiver.os, "getuid", lambda: 0)
    monkeypatch.setattr(receiver, "Path", fake_path)

    path = latency_socket_path({"SUDO_UID": "1000"})

    assert path == run_user / "penguin-burner" / "latency.sock"


def test_latency_socket_paths_include_home_visible_fallback(tmp_path) -> None:
    runtime_dir = tmp_path / "run" / "user" / "1000"
    home = tmp_path / "home" / "desktop"

    paths = latency_socket_paths(
        {
            "XDG_RUNTIME_DIR": str(runtime_dir),
            "HOME": str(home),
        }
    )

    assert paths == [
        runtime_dir / "penguin-burner" / "latency.sock",
        home / ".cache" / "penguin-burner" / "latency.sock",
    ]


def test_latency_logger_socket_is_world_writable(tmp_path) -> None:
    socket_path = tmp_path / "latency.sock"
    logger = LatencyTelemetryLogger(log=lambda _line: None, path=socket_path).start()
    try:
        assert socket_path.stat().st_mode & 0o777 == 0o666
    finally:
        logger.close()


def test_latency_logger_binds_multiple_sockets(tmp_path) -> None:
    first = tmp_path / "run" / "latency.sock"
    second = tmp_path / "home" / "latency.sock"
    logger = LatencyTelemetryLogger(
        log=lambda _line: None,
        paths=[first, second],
    ).start()
    try:
        assert first.stat().st_mode & 0o777 == 0o666
        assert second.stat().st_mode & 0o777 == 0o666
    finally:
        logger.close()


def _marker_proxy_sample(**spans: int) -> dict:
    sample = {
        "type": "timing",
        "measurement": "marker-proxy",
        "pid": 123,
        "quality": "reflex-markers",
        "marker_bits": 1,
    }
    sample.update(spans)
    return sample


def test_latency_meter_selects_sim_to_present_tier_without_input_marker() -> None:
    meter = LatencyTelemetryMeter(time_monotonic=lambda: 100.0)
    for span_us in (30000, 32000, 34000, 36000):
        meter.add_sample(
            _marker_proxy_sample(
                sim_to_present_us=span_us,
                submit_to_present_us=span_us - 8000,
            )
        )

    snapshot = meter.snapshot(now=100.5)

    assert snapshot is not None
    assert snapshot["latency_quality"] == "sim-to-present"
    assert snapshot["latency_proxy_p95_us"] == 36000
    assert snapshot["latency_p95_ms"] == 36.0
    assert snapshot["sim_to_present_p95_us"] == 36000
    assert snapshot["submit_to_present_p95_us"] == 28000
    assert snapshot["best_quality"] == "reflex-marker-sim-present"


def test_latency_meter_prefers_input_present_over_sim_present() -> None:
    meter = LatencyTelemetryMeter(time_monotonic=lambda: 100.0)
    meter.add_sample(
        _marker_proxy_sample(
            input_to_present_us=40000,
            sim_to_present_us=33000,
        )
    )

    snapshot = meter.snapshot(now=100.5)

    assert snapshot is not None
    assert snapshot["latency_quality"] == "marker-input-to-present"
    assert snapshot["latency_proxy_p95_us"] == 40000
    assert snapshot["best_quality"] == "reflex-marker-input-present"


def test_latency_meter_falls_back_to_oob_present_span() -> None:
    meter = LatencyTelemetryMeter(time_monotonic=lambda: 100.0)
    meter.add_sample(_marker_proxy_sample(sim_to_oob_present_us=31000))

    snapshot = meter.snapshot(now=100.5)

    assert snapshot is not None
    assert snapshot["latency_quality"] == "sim-to-oob-present"
    assert snapshot["latency_proxy_p95_us"] == 31000
    assert snapshot["best_quality"] == "reflex-marker-sim-present"


def test_latency_meter_prefers_input_oob_over_sim_oob_when_framegen_active() -> None:
    # Title emits INPUT_SAMPLE: under frame generation the widest input-anchored
    # span (input -> displayed present) wins over the sim-anchored one.
    meter = LatencyTelemetryMeter(time_monotonic=lambda: 100.0)
    meter.add_sample(
        _marker_proxy_sample(
            input_to_present_us=24000,
            sim_to_present_us=22000,
            sim_to_oob_present_us=60000,
            input_to_oob_present_us=64000,
            framegen_active=True,
        )
    )

    snapshot = meter.snapshot(now=100.5)

    assert snapshot is not None
    assert snapshot["latency_quality"] == "input-to-oob-present"
    assert snapshot["latency_proxy_p95_us"] == 64000


def test_latency_meter_prefers_oob_present_when_framegen_active() -> None:
    # Under frame generation the sim/input->app-present spans end before the
    # generated-frame pacing hold, so they read unrealistically low. The wider
    # sim->out-of-band-present span (to the actual display hand-off) must win.
    meter = LatencyTelemetryMeter(time_monotonic=lambda: 100.0)
    meter.add_sample(
        _marker_proxy_sample(
            input_to_present_us=20000,
            sim_to_present_us=18000,
            sim_to_oob_present_us=62000,
            framegen_active=True,
        )
    )

    snapshot = meter.snapshot(now=100.5)

    assert snapshot is not None
    assert snapshot["latency_quality"] == "sim-to-oob-present"
    assert snapshot["latency_proxy_p95_us"] == 62000
    assert snapshot["latency_p95_ms"] == 62.0


def test_latency_meter_falls_back_to_submit_to_present_span() -> None:
    meter = LatencyTelemetryMeter(time_monotonic=lambda: 100.0)
    meter.add_sample(_marker_proxy_sample(submit_to_present_us=22000))

    snapshot = meter.snapshot(now=100.5)

    assert snapshot is not None
    assert snapshot["latency_quality"] == "submit-to-present"
    assert snapshot["latency_proxy_p95_us"] == 22000
    assert snapshot["best_quality"] == "reflex-marker-submit-present"


def test_latency_meter_summary_includes_cross_phase_columns() -> None:
    meter = LatencyTelemetryMeter(time_monotonic=lambda: 100.0)
    for span_us in (30000, 32000, 34000, 36000):
        meter.add_sample(_marker_proxy_sample(sim_to_present_us=span_us))

    summary = meter.summary(now=100.5)

    assert summary is not None
    assert "latency-proxy-p95=36.00ms" in summary
    assert "latency-quality=sim-to-present" in summary
    assert "sim-to-present-p95=36.00ms" in summary
    assert "submit-to-present-p95=n/a" in summary


def test_latency_meter_latency_fields_are_none_without_latency_samples() -> None:
    meter = LatencyTelemetryMeter(time_monotonic=lambda: 100.0)
    meter.add_sample(
        {
            "type": "timing",
            "measurement": "present-pacing",
            "pid": 123,
            "quality": "present-frametime",
            "present_frametime_us": 16600,
        }
    )

    snapshot = meter.snapshot(now=100.5)

    assert snapshot is not None
    assert snapshot["latency_p95_ms"] is None
    assert snapshot["latency_quality"] is None
    summary = meter.summary(now=100.5)
    assert summary is not None
    assert "latency-quality" not in summary


def _driver_report_sample(present_id: int, **extra) -> dict:
    sample = {
        "type": "timing",
        "measurement": "driver-report",
        "pid": 123,
        "quality": "reflex-render-submit",
        "present_id": present_id,
        "render_submit_us": 6300,
        "driver_report_duplicate_count": 0,
    }
    sample.update(extra)
    return sample


def test_latency_meter_rejects_cycling_stale_driver_ring() -> None:
    meter = LatencyTelemetryMeter(time_monotonic=lambda: 100.0)
    ring_ids = (618, 619, 620, 621)
    for present_id in ring_ids:
        meter.add_sample(_driver_report_sample(present_id))
    # The stale ring re-presents the same IDs; the layer's own duplicate
    # counter resets on every ID change, so each repeat still claims 0.
    for _ in range(3):
        for present_id in ring_ids:
            meter.add_sample(_driver_report_sample(present_id))

    snapshot = meter.snapshot(now=100.5)

    assert snapshot is not None
    assert len(snapshot["samples"]) == len(ring_ids)
    assert snapshot["latency_quality"] == "render-submit"


def test_latency_meter_keeps_advancing_driver_reports_fresh() -> None:
    meter = LatencyTelemetryMeter(time_monotonic=lambda: 100.0)
    for present_id in (100, 101, 102, 103):
        meter.add_sample(_driver_report_sample(present_id))

    snapshot = meter.snapshot(now=100.5)

    assert snapshot is not None
    assert len(snapshot["samples"]) == 4


def test_latency_meter_accepts_present_id_reset_after_swapchain_recreation() -> None:
    meter = LatencyTelemetryMeter(time_monotonic=lambda: 100.0)
    meter.add_sample(_driver_report_sample(5000))
    meter.add_sample(_driver_report_sample(2))
    meter.add_sample(_driver_report_sample(3))

    snapshot = meter.snapshot(now=100.5)

    assert snapshot is not None
    assert len(snapshot["samples"]) == 3


class _FakeSocket:
    def __init__(self) -> None:
        self.samples: list[dict] = []

    def sendto(self, line: bytes, target: object) -> None:
        self.samples.append(json.loads(line.decode("utf-8")))


def test_marker_bridge_parses_out_of_band_present_end_trace() -> None:
    line = (
        "12345.678: info:  NvAPI_D3D12_SetAsyncFrameMarker: "
        "frameID=42,markerType=OUT_OF_BAND_PRESENT_END,presentFrameID=99"
    )
    assert marker_bridge._parse_line(line) == (
        42,
        marker_bridge.NV_MARKER_OUT_OF_BAND_PRESENT_END,
        (12345 * 1000 + 678) * 1000,
    )


def test_marker_bridge_resolve_oob_emits_framegen_inclusive_span() -> None:
    sock = _FakeSocket()
    # Base frame 7: no input marker (input_us=0), sim at 1_000_000us, app present
    # completes 18ms later.
    awaiting = [(7, 0, 1_000_000, 1_018_000)]
    # The generated-frame display present lands 60ms after simulation.
    emitted = marker_bridge._resolve_oob_present(
        sock, [Path("/unused.sock")], awaiting, 1_060_000, pid=99
    )

    assert emitted == 1
    assert awaiting == []
    assert len(sock.samples) == 1
    sample = sock.samples[0]
    assert sample["present_id"] == 7
    assert sample["sim_to_oob_present_us"] == 60000
    assert sample["sim_to_present_us"] == 18000
    # No input marker -> no input-anchored spans.
    assert "input_to_oob_present_us" not in sample
    assert "input_to_present_us" not in sample
    # The displayed-present span feeds the latency ladder; it does not assert
    # frame generation (out-of-band presents also occur with frame gen off).
    assert sample["framegen_active"] is False


def test_marker_bridge_resolve_oob_emits_input_anchored_spans() -> None:
    sock = _FakeSocket()
    # Base frame 7: input sampled at 0.996ms before sim, sim at 1_000_000us,
    # app present at 1_018_000us, displayed present at 1_060_000us.
    awaiting = [(7, 996_000, 1_000_000, 1_018_000)]
    emitted = marker_bridge._resolve_oob_present(
        sock, [Path("/unused.sock")], awaiting, 1_060_000, pid=99
    )

    assert emitted == 1
    sample = sock.samples[0]
    assert sample["input_to_present_us"] == 1_018_000 - 996_000  # 22000
    assert sample["input_to_oob_present_us"] == 1_060_000 - 996_000  # 64000
    assert sample["sim_to_oob_present_us"] == 60000


def test_marker_bridge_resolve_oob_drops_unrelated_present() -> None:
    sock = _FakeSocket()
    awaiting = [(7, 0, 1_000_000, 1_018_000)]
    # 282ms later: beyond _MAX_OOB_LAG_US, so this is not frame 7's display.
    emitted = marker_bridge._resolve_oob_present(
        sock, [Path("/unused.sock")], awaiting, 1_300_000, pid=99
    )

    assert emitted == 0
    assert awaiting == []
    assert sock.samples == []


def test_marker_bridge_resolve_oob_waits_for_future_base_frame() -> None:
    sock = _FakeSocket()
    awaiting = [(8, 0, 2_000_000, 2_050_000)]
    # This out-of-band present precedes the base frame's app present; keep waiting.
    emitted = marker_bridge._resolve_oob_present(
        sock, [Path("/unused.sock")], awaiting, 2_030_000, pid=99
    )

    assert emitted == 0
    assert awaiting == [(8, 0, 2_000_000, 2_050_000)]
    assert sock.samples == []
