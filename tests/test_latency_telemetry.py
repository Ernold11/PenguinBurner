from pathlib import Path

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
                "marker_name": "oob-present-start",
            }
        )

    snapshot = meter.snapshot(now=103.0)

    assert snapshot is not None
    assert snapshot["present_frametime_p95_ms"] == 8.333
    assert snapshot["present_fps"] == "40"
    assert snapshot["base_present_fps"] == 40
    assert snapshot["base_present_frametime_p95_ms"] == 25.0
    assert snapshot["raw_present_fps_stats"]["avg"] == "120"


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
    home = tmp_path / "home" / "jp"

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
