from latency_telemetry.flow_analysis import (
    analyze_latency_flow_lines,
    parse_latency_log_line,
)


def test_parse_latency_log_line_reads_receiver_key_values() -> None:
    sample = parse_latency_log_line(
        "2026-06-09 21:05:00 event=latency-layer-status "
        "status=create-swapchain pid=123 live_swapchain_count=1 "
        "present_mode_name=IMMEDIATE swapchain_latency_mode=True"
    )

    assert sample == {
        "event": "latency-layer-status",
        "status": "create-swapchain",
        "pid": 123,
        "live_swapchain_count": 1,
        "present_mode_name": "IMMEDIATE",
        "swapchain_latency_mode": True,
    }


def test_parse_latency_log_line_reads_raw_json_event() -> None:
    sample = parse_latency_log_line(
        '{"type":"status","event":"latency-stream-stale",'
        '"live_swapchain_count":1,"swapchain_latency_mode":true}'
    )

    assert sample == {
        "type": "status",
        "event": "latency-stream-stale",
        "live_swapchain_count": 1,
        "swapchain_latency_mode": True,
    }


def test_flow_analysis_flags_vkd3d_multi_swapchain_guard() -> None:
    diagnosis = analyze_latency_flow_lines(
        [
            "2026-06-09 21:05:00 event=latency-layer-status "
            "status=create-swapchain live_swapchain_count=1 "
            "present_mode_name=IMMEDIATE swapchain_latency_mode=True",
            "2026-06-09 21:05:01 event=latency-layer-status "
            "status=create-swapchain live_swapchain_count=2 "
            "present_mode_name=IMMEDIATE swapchain_latency_mode=True",
            "2026-06-09 21:05:02 event=latency-layer-status "
            "status=latency-stream-stale live_swapchain_count=2 "
            "swapchain_latency_mode=True present_count=512 "
            "last_vulkan_present_id=512 latest_marker_present_id=512 "
            "last_driver_report_present_id=470 "
            "driver_report_duplicate_count=240",
        ]
    )

    assert diagnosis.root_cause == "vkd3d-multi-swapchain-reflex-guard"
    assert "vk_swapchain_count > 1" in diagnosis.summary
    assert diagnosis.stats["immediate_seen"] is True


def test_flow_analysis_flags_driver_ring_stale_after_markers_and_presents() -> None:
    diagnosis = analyze_latency_flow_lines(
        [
            "2026-06-09 21:05:00 event=latency-layer-status "
            "status=create-swapchain live_swapchain_count=1 "
            "present_mode_name=IMMEDIATE swapchain_latency_mode=True",
            "2026-06-09 21:05:01 event=latency-raw measurement=marker-proxy "
            "present_id=512 timing_count=0 gpu_render_us=0 "
            "gpu_render_start_us=0 gpu_render_end_us=0",
            "2026-06-09 21:05:02 event=latency-layer-status "
            "status=latency-stream-stale live_swapchain_count=1 "
            "swapchain_latency_mode=True present_count=512 "
            "last_vulkan_present_id=510 latest_marker_present_id=512 "
            "last_input_sample_present_id=511 "
            "last_render_submit_present_id=512 "
            "last_driver_report_present_id=470 "
            "driver_report_duplicate_count=240",
        ]
    )

    assert diagnosis.root_cause == "nvidia-reflex-timing-ring-stale"
    assert "vkGetLatencyTimingsNV kept returning the same report" in diagnosis.summary
    assert "distinct_gpu_render_us=0" in diagnosis.evidence
    assert "raw_driver_timestamp_samples=0" in diagnosis.evidence


def test_flow_analysis_flags_stale_status_snapshot_without_stale_event() -> None:
    diagnosis = analyze_latency_flow_lines(
        [
            "2026-06-09 22:08:20 event=latency-layer-status "
            "status=latency-sleep live_swapchain_count=1 "
            "swapchain_latency_mode=True driver_report_duplicate_count=475 "
            "present_count=211702 last_vulkan_present_id=0 "
            "latest_marker_present_id=213907 "
            "last_render_submit_present_id=213907 "
            "last_present_marker_present_id=213906 "
            "last_driver_report_present_id=213258",
        ]
    )

    assert diagnosis.root_cause == "driver-report-stale-after-reflex-markers"
    assert any(
        "stale_events=0 stale_status_snapshots=1" in item
        for item in diagnosis.evidence
    )
    assert "latest_stale.source_status=latency-sleep" in diagnosis.evidence


def test_flow_analysis_treats_advancing_immediate_capture_as_candidate_workaround() -> None:
    lines = [
        "2026-06-09 21:05:00 event=latency-layer-status "
        "status=create-swapchain live_swapchain_count=1 "
        "present_mode_name=IMMEDIATE swapchain_latency_mode=True"
    ]
    lines.extend(
        f"2026-06-09 21:05:{present_id:02d} event=latency-raw "
        f"present_id={present_id} gpu_render_us={16000 + present_id} "
        "quality=reflex-render-submit"
        for present_id in range(1, 12)
    )

    diagnosis = analyze_latency_flow_lines(lines)

    assert diagnosis.root_cause == "no-stall-detected-with-immediate-present-mode"
    assert "menu-to-gameplay" in diagnosis.recommendation


def test_flow_analysis_flags_dxvk_driver_report_lag_then_stall() -> None:
    diagnosis = analyze_latency_flow_lines(
        [
            "Jun 09 22:44:04 home PenguinBurner[26343]: "
            "2026-06-09 22:44:04 event=latency-layer-status "
            "status=dxvk-driver-report-lag-selected pid=28769 count=120 "
            "device=0x1 swapchain=0x2",
            "Jun 09 22:44:04 home PenguinBurner[26343]: "
            "requested_present_id=2389 newest_driver_report_present_id=2391 "
            "selected_driver_report_present_id=2380 driver_report_lag_frames=9 "
            "timing_query_interval=4 last_driver_report_present_id=2379",
            "Jun 09 22:44:40 home PenguinBurner[26343]: "
            "2026-06-09 22:44:40 event=latency-layer-status "
            "status=dxvk-driver-report-miss pid=28769 count=3960 "
            "device=0x1 swapchain=0x2",
            "Jun 09 22:44:40 home PenguinBurner[26343]: "
            "driver_report_lag_frames=0 timing_query_interval=4 "
            "last_driver_report_present_id=5939",
            "2026-06-09 22:46:28 event=latency-meter "
            "quality=reflex-marker-render-submit samples=240 "
            "render-submit-p95=16.55ms gpu-render-p95=n/a "
            "present-frametime-p95=18.86ms present-fps=54 "
            "missing=input-sample,driver-timing",
        ]
    )

    assert diagnosis.root_cause == "dxvk-nvapi-driver-report-lagged-then-stalled"
    assert "pre-frame-generation base-frame cadence" in diagnosis.recommendation
    assert "latest_dxvk_lag.driver_report_lag_frames=9" in diagnosis.evidence
    assert "latest_dxvk_miss.last_driver_report_present_id=5939" in diagnosis.evidence
    assert diagnosis.stats["dxvk_driver_report_lag_selected"] == 1
    assert diagnosis.stats["dxvk_driver_report_miss"] == 1
    assert diagnosis.stats["present_fps_min"] == 54


def test_flow_analysis_labels_present_cadence_without_driver_timestamps() -> None:
    diagnosis = analyze_latency_flow_lines(
        [
            "2026-06-09 22:45:58 event=latency-raw measurement=marker-proxy "
            "present_id=10241 quality=reflex-render-submit "
            "render_submit_us=15388 driver_start_us=0 driver_end_us=0 "
            "gpu_render_start_us=0 gpu_render_end_us=0",
            "2026-06-09 22:46:08 event=latency-meter "
            "quality=reflex-marker-render-submit samples=240 "
            "render-submit-p95=16.29ms gpu-render-p95=n/a "
            "present-frametime-p95=18.85ms present-fps=54 "
            "missing=input-sample,driver-timing",
        ]
    )

    assert diagnosis.root_cause == "present-cadence-only-no-driver-gpu-timestamps"
    assert "base-frame pacing signal" in diagnosis.summary
    assert diagnosis.stats["present_fps_median"] == 54
    assert any("present_fps_median=54" in item for item in diagnosis.evidence)


def test_flow_analysis_reports_empty_input() -> None:
    diagnosis = analyze_latency_flow_lines([])

    assert diagnosis.root_cause == "no-latency-flow-events"
