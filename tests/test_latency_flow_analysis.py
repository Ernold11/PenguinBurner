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


def test_flow_analysis_reports_empty_input() -> None:
    diagnosis = analyze_latency_flow_lines([])

    assert diagnosis.root_cause == "no-latency-flow-events"
