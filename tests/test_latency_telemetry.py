import latency_telemetry.receiver as receiver
from latency_telemetry.receiver import (
    LatencyTelemetryLogger,
    LatencyTelemetryMeter,
    latency_socket_path,
    latency_socket_paths,
)


def test_latency_telemetry_meter_formats_reflex_summary() -> None:
    now = 100.0
    meter = LatencyTelemetryMeter(time_monotonic=lambda: now)
    meter.add_sample(
        {
            "type": "timing",
            "pid": 123,
            "present_id": 7,
            "quality": "reflex-markers",
            "gpu_frame_time_us": 16667,
            "input_to_present_us": 32000,
            "render_submit_us": 2100,
            "render_present_us": 12800,
        }
    )

    summary = meter.summary(now=101.25)

    assert summary is not None
    assert "event=latency-meter" in summary
    assert "quality=reflex-input-present" in summary
    assert "pid=123" in summary
    assert "latency-proxy-p95=32.00ms" in summary
    assert "render-submit-p95=2.10ms" in summary
    assert "render-present-p95=12.80ms" in summary
    assert "gpu-frame-p95=16.67ms" in summary
    assert "input-present-p95=32.00ms" in summary
    assert "missing=" not in summary


def test_latency_telemetry_meter_reports_present_pacing_without_reflex() -> None:
    now = 100.0
    meter = LatencyTelemetryMeter(time_monotonic=lambda: now)
    # No Reflex markers, no driver timing — only present-to-present pacing,
    # the signal available for any Vulkan app regardless of Reflex.
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
    assert "quality=present-frametime" in summary
    # p95 frametime is dominated by the slow 50 ms frame.
    assert "present-frametime-p95=50.00ms" in summary
    # FPS is derived from the typical (median) frametime, not the p95 tail,
    # so a single hitch does not crater the reported rate.
    assert "present-fps=60" in summary


def test_latency_telemetry_meter_reports_gpu_render_time() -> None:
    now = 100.0
    meter = LatencyTelemetryMeter(time_monotonic=lambda: now)
    meter.add_sample(
        {
            "type": "timing",
            "measurement": "driver-report",
            "pid": 123,
            "present_id": 7,
            "quality": "reflex-render-submit",
            "driver_report_duplicate_count": 0,
            "render_submit_us": 2100,
            "gpu_render_us": 16600,
        }
    )

    summary = meter.summary(now=101.25)

    assert summary is not None
    assert "gpu-render-p95=16.60ms" in summary


def test_latency_telemetry_meter_normalizes_dxvk_nvapi_driver_report() -> None:
    now = 100.0
    meter = LatencyTelemetryMeter(time_monotonic=lambda: now)
    meter.add_sample(
        {
            "type": "timing",
            "source": "dxvk-nvapi-vkreflex",
            "pid": 123,
            "device": "0x1",
            "swapchain": "0x2",
            "present_id": 7,
            "quality": "reflex-render-submit",
            "timing_count": 8,
            "render_submit_us": 2100,
            "gpu_render_start_us": 10000,
            "gpu_render_end_us": 26600,
        }
    )

    summary = meter.summary(now=101.25)

    assert summary is not None
    assert "quality=reflex-render-submit" in summary
    assert "gpu-render-p95=16.60ms" in summary
    assert "missing=" not in summary


def test_latency_telemetry_meter_marks_repeated_dxvk_nvapi_report_stale() -> None:
    now = 100.0
    meter = LatencyTelemetryMeter(time_monotonic=lambda: now)
    sample = {
        "type": "timing",
        "source": "dxvk-nvapi-vkreflex",
        "pid": 123,
        "device": "0x1",
        "swapchain": "0x2",
        "present_id": 7,
        "quality": "reflex-render-submit",
        "timing_count": 8,
        "render_submit_us": 7000,
        "gpu_render_start_us": 10000,
        "gpu_render_end_us": 26600,
    }
    first_stored = meter.add_sample(sample)

    now = 106.0
    second_stored = meter.add_sample(sample)

    summary = meter.summary(now=106.0)

    assert first_stored is not None
    assert first_stored["measurement"] == "driver-report"
    assert first_stored["driver_report_duplicate_count"] == 0
    assert second_stored is not None
    assert second_stored["driver_report_duplicate_count"] == 1
    assert summary is not None
    assert "quality=stale-driver-report" in summary
    assert "samples=0" in summary
    assert "stale-present_id=7" in summary
    assert "stale-driver-report-duplicates=1" in summary


def test_latency_telemetry_meter_reports_render_submit_only_missing_inputs() -> None:
    now = 100.0
    meter = LatencyTelemetryMeter(time_monotonic=lambda: now)
    meter.add_sample(
        {
            "type": "timing",
            "pid": 123,
            "present_id": 7,
            "quality": "reflex-markers",
            "marker_bits": (1 << 2) | (1 << 3),
            "vk_nv_low_latency2_functions": True,
            "render_submit_us": 7990,
            "input_to_present_us": 0,
            "gpu_frame_time_us": 0,
            "gpu_render_start_us": 0,
            "gpu_render_end_us": 0,
            "driver_start_us": 0,
            "driver_end_us": 0,
        }
    )

    summary = meter.summary(now=101.25)

    assert summary is not None
    assert "quality=reflex-render-submit" in summary
    assert "latency-proxy-p95=7.99ms" in summary
    assert "render-submit-p95=7.99ms" in summary
    assert "input-present-p95=n/a" in summary
    assert "gpu-frame-p95=n/a" in summary
    assert "missing=input-sample,driver-timing" in summary


def test_latency_telemetry_meter_labels_marker_proxy_without_promoting_to_driver_latency() -> None:
    now = 100.0
    meter = LatencyTelemetryMeter(time_monotonic=lambda: now)
    meter.add_sample(
        {
            "type": "timing",
            "pid": 123,
            "present_id": 7,
            "quality": "reflex-marker-input-present",
            "measurement": "marker-proxy",
            "marker_bits": (1 << 4) | (1 << 5) | (1 << 6),
            "vk_nv_low_latency2_functions": True,
            "render_submit_us": 0,
            "input_to_present_us": 27000,
            "gpu_frame_time_us": 0,
            "gpu_render_start_us": 0,
            "gpu_render_end_us": 0,
            "driver_start_us": 0,
            "driver_end_us": 0,
        }
    )

    summary = meter.summary(now=101.25)

    assert summary is not None
    assert "quality=reflex-marker-input-present" in summary
    assert "latency-proxy-p95=27.00ms" in summary
    assert "input-present-p95=27.00ms" in summary
    assert "gpu-frame-p95=n/a" in summary


def test_latency_telemetry_meter_ignores_duplicate_driver_reports() -> None:
    now = 100.0
    meter = LatencyTelemetryMeter(time_monotonic=lambda: now)
    meter.add_sample(
        {
            "type": "timing",
            "measurement": "driver-report",
            "pid": 123,
            "present_id": 7,
            "quality": "reflex-render-submit",
            "driver_report_duplicate_count": 0,
            "render_submit_us": 7000,
        }
    )
    meter.add_sample(
        {
            "type": "timing",
            "measurement": "driver-report",
            "pid": 123,
            "present_id": 7,
            "quality": "reflex-render-submit",
            "driver_report_duplicate_count": 1,
            "render_submit_us": 99000,
        }
    )

    summary = meter.summary(now=101.25)

    assert summary is not None
    assert "samples=1" in summary
    assert "render-submit-p95=7.00ms" in summary


def test_latency_telemetry_meter_marks_stale_duplicate_driver_reports() -> None:
    now = 100.0
    meter = LatencyTelemetryMeter(time_monotonic=lambda: now)
    meter.add_sample(
        {
            "type": "timing",
            "measurement": "driver-report",
            "pid": 123,
            "present_id": 102,
            "quality": "reflex-render-submit",
            "driver_report_duplicate_count": 0,
            "render_submit_us": 7000,
            "render_present_us": 32000,
        }
    )

    now = 106.0
    meter.add_sample(
        {
            "type": "timing",
            "measurement": "driver-report",
            "pid": 123,
            "present_id": 476,
            "quality": "reflex-render-submit",
            "driver_report_duplicate_count": 9000,
            "render_submit_us": 99000,
            "render_present_us": 24000,
        }
    )

    summary = meter.summary(now=106.0)

    assert summary is not None
    assert "quality=stale-driver-report" in summary
    assert "samples=0" in summary
    assert "render-submit-p95=n/a" in summary
    assert "stale-present_id=476" in summary
    assert "stale-driver-report-duplicates=9000" in summary
    assert "missing=fresh-samples" in summary


def test_latency_telemetry_meter_marks_stale_reflex_with_present_pacing_fallback() -> None:
    now = 100.0
    meter = LatencyTelemetryMeter(time_monotonic=lambda: now)
    meter.add_sample(
        {
            "type": "timing",
            "measurement": "driver-report",
            "pid": 123,
            "present_id": 497,
            "quality": "reflex-render-submit",
            "driver_report_duplicate_count": 0,
            "render_submit_us": 7935,
            "gpu_render_us": 8469,
        }
    )

    now = 106.0
    meter.add_sample(
        {
            "type": "timing",
            "measurement": "driver-report",
            "pid": 123,
            "present_id": 497,
            "quality": "reflex-render-submit",
            "driver_report_duplicate_count": 6717,
            "render_submit_us": 7935,
            "gpu_render_us": 8469,
        }
    )
    for frametime_us in (27000, 29000, 28000):
        meter.add_sample(
            {
                "type": "timing",
                "measurement": "present-pacing",
                "pid": 123,
                "quality": "present-frametime",
                "present_frametime_us": frametime_us,
            }
        )

    summary = meter.summary(now=106.0)

    assert summary is not None
    assert "quality=stale-driver-report" in summary
    assert "samples=3" in summary
    assert "gpu-render-p95=n/a" in summary
    assert "present-frametime-p95=29.00ms" in summary
    assert "present-fps=36" in summary
    assert "stale-present_id=497" in summary
    assert "stale-driver-report-duplicates=6717" in summary


def test_latency_telemetry_logger_formats_status_events() -> None:
    logs: list[str] = []
    logger = LatencyTelemetryLogger(
        log=logs.append,
        time_strftime=lambda _format: "2026-06-03 22:00:00",
    )

    logger._log_status(
        {
            "type": "status",
            "event": "latency-timing-unavailable",
            "pid": 123,
            "count": 2,
            "device": "0x1",
            "swapchain": "0x2",
            "vk_nv_low_latency2_advertised": True,
            "vk_nv_low_latency2_requested": False,
            "vk_nv_low_latency2_functions": False,
            "marker_count": 0,
        }
    )

    assert logs == [
        "2026-06-03 22:00:00 event=latency-layer-status "
        "status=latency-timing-unavailable pid=123 count=2 device=0x1 "
        "swapchain=0x2 vk_nv_low_latency2_advertised=True "
        "vk_nv_low_latency2_requested=False vk_nv_low_latency2_functions=False "
        "marker_count=0"
    ]


def test_latency_telemetry_logger_formats_swapchain_lifecycle_status_event() -> None:
    logs: list[str] = []
    logger = LatencyTelemetryLogger(
        log=logs.append,
        time_strftime=lambda _format: "2026-06-03 22:00:00",
    )

    logger._log_status(
        {
            "type": "status",
            "event": "create-swapchain",
            "pid": 123,
            "count": 1,
            "live_swapchain_count": 2,
            "device": "0x1",
            "swapchain": "0x2",
            "old_swapchain": "0x9",
            "min_image_count": 3,
            "image_width": 3840,
            "image_height": 2160,
            "image_format": 44,
            "present_mode": 0,
            "present_mode_name": "IMMEDIATE",
            "swapchain_latency_mode": True,
        }
    )

    assert logs == [
        "2026-06-03 22:00:00 event=latency-layer-status "
        "status=create-swapchain pid=123 count=1 live_swapchain_count=2 "
        "device=0x1 swapchain=0x2 old_swapchain=0x9 min_image_count=3 "
        "image_width=3840 image_height=2160 image_format=44 present_mode=0 "
        "present_mode_name=IMMEDIATE swapchain_latency_mode=True"
    ]


def test_latency_telemetry_logger_formats_recovery_status_events() -> None:
    logs: list[str] = []
    logger = LatencyTelemetryLogger(
        log=logs.append,
        time_strftime=lambda _format: "2026-06-03 22:00:00",
    )

    logger._log_status(
        {
            "type": "status",
            "event": "latency-recovery-reapply-sleep-mode",
            "pid": 123,
            "count": 1,
            "result": 0,
            "device": "0x1",
            "swapchain": "0x2",
            "has_latency_sleep_mode": True,
            "low_latency_mode": True,
            "low_latency_boost": False,
            "minimum_interval_us": 750,
            "driver_report_duplicate_count": 240,
            "present_count": 512,
        }
    )

    assert logs == [
        "2026-06-03 22:00:00 event=latency-layer-status "
        "status=latency-recovery-reapply-sleep-mode pid=123 count=1 result=0 "
        "device=0x1 swapchain=0x2 has_latency_sleep_mode=True "
        "low_latency_mode=True low_latency_boost=False minimum_interval_us=750 "
        "driver_report_duplicate_count=240 present_count=512"
    ]


def test_latency_telemetry_logger_formats_recovery_disable_status_event() -> None:
    logs: list[str] = []
    logger = LatencyTelemetryLogger(
        log=logs.append,
        time_strftime=lambda _format: "2026-06-03 22:00:00",
    )

    logger._log_status(
        {
            "type": "status",
            "event": "latency-recovery-disable-sleep-mode",
            "pid": 123,
            "count": 2,
            "result": 0,
            "device": "0x1",
            "swapchain": "0x2",
            "has_latency_sleep_mode": True,
            "low_latency_mode": False,
            "low_latency_boost": False,
            "minimum_interval_us": 0,
            "driver_report_duplicate_count": 840,
            "present_count": 2048,
        }
    )

    assert logs == [
        "2026-06-03 22:00:00 event=latency-layer-status "
        "status=latency-recovery-disable-sleep-mode pid=123 count=2 result=0 "
        "device=0x1 swapchain=0x2 has_latency_sleep_mode=True "
        "low_latency_mode=False low_latency_boost=False minimum_interval_us=0 "
        "driver_report_duplicate_count=840 present_count=2048"
    ]


def test_latency_telemetry_logger_formats_flow_status_event() -> None:
    logs: list[str] = []
    logger = LatencyTelemetryLogger(
        log=logs.append,
        time_strftime=lambda _format: "2026-06-03 22:00:00",
    )

    logger._log_status(
        {
            "type": "status",
            "event": "latency-stream-stale",
            "pid": 123,
            "count": 1,
            "result": 1,
            "device": "0x1",
            "swapchain": "0x2",
            "queue": "0x3",
            "queue_type": 2,
            "sleep_value": 99,
            "live_swapchain_count": 1,
            "swapchain_latency_mode": True,
            "present_count": 512,
            "last_vulkan_present_id": 510,
            "latest_marker_present_id": 512,
            "last_input_sample_present_id": 511,
            "last_simulation_present_id": 512,
            "last_render_submit_present_id": 512,
            "last_present_marker_present_id": 512,
            "last_oob_render_submit_present_id": 512,
            "last_oob_present_present_id": 512,
            "last_driver_report_present_id": 470,
            "driver_report_duplicate_count": 240,
            "marker_count": 4096,
        }
    )

    assert logs == [
        "2026-06-03 22:00:00 event=latency-layer-status "
        "status=latency-stream-stale pid=123 count=1 live_swapchain_count=1 result=1 "
        "device=0x1 swapchain=0x2 queue=0x3 queue_type=2 sleep_value=99 "
        "swapchain_latency_mode=True driver_report_duplicate_count=240 present_count=512 "
        "last_vulkan_present_id=510 latest_marker_present_id=512 "
        "last_input_sample_present_id=511 last_simulation_present_id=512 "
        "last_render_submit_present_id=512 last_present_marker_present_id=512 "
        "last_oob_render_submit_present_id=512 last_oob_present_present_id=512 "
        "last_driver_report_present_id=470 marker_count=4096"
    ]


def test_latency_telemetry_logger_formats_raw_timing_events() -> None:
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
            "measurement": "driver-report",
            "pid": 123,
            "device": "0x1",
            "swapchain": "0x2",
            "present_id": 99,
            "quality": "reflex-render-submit",
            "sample_count": 4,
            "timing_count": 8,
            "driver_report_count": 5,
            "driver_report_duplicate_count": 0,
            "marker_bits": 48,
            "render_submit_us": 7900,
            "render_present_us": 24230,
            "input_to_present_us": 0,
            "gpu_frame_time_us": 0,
            "gpu_render_us": 16600,
            "input_sample_us": 0,
            "sim_start_us": 100,
            "sim_end_us": 200,
            "render_submit_start_us": 300,
            "render_submit_end_us": 8200,
            "present_start_us": 9000,
            "present_end_us": 10000,
            "driver_start_us": 0,
            "driver_end_us": 0,
            "os_render_queue_start_us": 0,
            "os_render_queue_end_us": 0,
            "gpu_render_start_us": 0,
            "gpu_render_end_us": 33230,
        }
    )

    assert logs == [
        "2026-06-03 22:00:00 event=latency-raw pid=123 "
        "measurement=driver-report device=0x1 swapchain=0x2 present_id=99 "
        "quality=reflex-render-submit sample_count=4 timing_count=8 "
        "driver_report_count=5 driver_report_duplicate_count=0 marker_bits=48 "
        "render_submit_us=7900 "
        "render_present_us=24230 input_to_present_us=0 gpu_frame_time_us=0 "
        "gpu_render_us=16600 "
        "input_sample_us=0 sim_start_us=100 sim_end_us=200 "
        "render_submit_start_us=300 render_submit_end_us=8200 "
        "present_start_us=9000 present_end_us=10000 driver_start_us=0 "
        "driver_end_us=0 os_render_queue_start_us=0 os_render_queue_end_us=0 "
        "gpu_render_start_us=0 gpu_render_end_us=33230"
    ]


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
