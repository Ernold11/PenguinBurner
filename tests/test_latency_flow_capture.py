from datetime import datetime

import latency_telemetry.flow_capture as flow_capture
from latency_telemetry.flow_capture import (
    default_capture_path,
    iter_filtered_latency_flow_lines,
    is_latency_flow_line,
    write_analysis,
    write_filtered_lines,
)


def test_latency_flow_capture_filter_keeps_only_flow_lines() -> None:
    assert is_latency_flow_line("status=create-swapchain present_mode_name=IMMEDIATE")
    assert is_latency_flow_line("event=latency-raw present_id=12 gpu_render_us=16000")
    assert is_latency_flow_line("status=latency-stream-stale live_swapchain_count=1")
    assert is_latency_flow_line("status=dxvk-driver-report-miss count=120")
    assert not is_latency_flow_line("2026-06-09 temp=50C fan=30% power=125W")


def test_default_capture_path_is_timestamped_under_cache_dir(tmp_path) -> None:
    path = default_capture_path(
        now=datetime(2026, 6, 9, 21, 30, 5),
        base_dir=tmp_path,
        prefix="re9",
    )

    assert path == tmp_path / "re9-20260609-213005.log"


def test_write_filtered_lines_writes_only_latency_flow(tmp_path) -> None:
    path = tmp_path / "capture.log"

    count = write_filtered_lines(
        [
            "2026-06-09 temp=50C fan=30%\n",
            "2026-06-09 event=latency-layer-status status=create-swapchain\n",
            "2026-06-09 event=latency-meter gpu-render-p95=16.00ms\n",
        ],
        path,
        sync=False,
    )

    assert count == 2
    assert path.read_text(encoding="utf-8").splitlines() == [
        "2026-06-09 event=latency-layer-status status=create-swapchain",
        "2026-06-09 event=latency-meter gpu-render-p95=16.00ms",
    ]


def test_filtered_lines_fold_latency_continuations() -> None:
    lines = list(
        iter_filtered_latency_flow_lines(
            [
                "2026-06-09 event=latency-layer-status status=latency-stream-stale "
                "live_swapchain_count=1\n",
                "swapchain=0xabc driver_report_duplicate_count=240\n",
                "present_count=512 latest_marker_present_id=900\n",
                "last_driver_report_present_id=470 marker_count=1234\n",
                "2026-06-09 temp=50C fan=30%\n",
                "2026-06-09 event=latency-meter quality=reflex-render-submit\n",
                "render-present-p95=n/a gpu-render-p95=n/a missing=gpu-frame\n",
            ]
        )
    )

    assert lines == [
        "2026-06-09 event=latency-layer-status status=latency-stream-stale "
        "live_swapchain_count=1 swapchain=0xabc "
        "driver_report_duplicate_count=240 present_count=512 "
        "latest_marker_present_id=900 last_driver_report_present_id=470 "
        "marker_count=1234\n",
        "2026-06-09 event=latency-meter quality=reflex-render-submit "
        "render-present-p95=n/a gpu-render-p95=n/a missing=gpu-frame\n",
    ]


def test_filtered_lines_fold_raw_timing_continuations() -> None:
    lines = list(
        iter_filtered_latency_flow_lines(
            [
                "2026-06-09 event=latency-raw measurement=marker-proxy "
                "present_id=6613\n",
                "quality=reflex-render-submit sample_count=840 timing_count=0 "
                "driver_report_count=2 driver_report_duplicate_count=840 "
                "marker_bits=63 render_submit_us=608\n",
                "gpu_render_start_us=0 gpu_render_end_us=0 driver_start_us=0 "
                "driver_end_us=0\n",
            ]
        )
    )

    assert lines == [
        "2026-06-09 event=latency-raw measurement=marker-proxy "
        "present_id=6613 quality=reflex-render-submit sample_count=840 "
        "timing_count=0 driver_report_count=2 "
        "driver_report_duplicate_count=840 marker_bits=63 "
        "render_submit_us=608 gpu_render_start_us=0 gpu_render_end_us=0 "
        "driver_start_us=0 driver_end_us=0\n",
    ]


def test_filtered_lines_fold_dxvk_driver_report_continuations() -> None:
    lines = list(
        iter_filtered_latency_flow_lines(
            [
                "2026-06-09 event=latency-layer-status "
                "status=dxvk-driver-report-lag-selected pid=28769 count=120\n",
                "requested_present_id=2389 newest_driver_report_present_id=2391 "
                "selected_driver_report_present_id=2380\n",
                "driver_report_lag_frames=9 timing_query_interval=4 "
                "last_driver_report_present_id=2379\n",
            ]
        )
    )

    assert lines == [
        "2026-06-09 event=latency-layer-status "
        "status=dxvk-driver-report-lag-selected pid=28769 count=120 "
        "requested_present_id=2389 newest_driver_report_present_id=2391 "
        "selected_driver_report_present_id=2380 "
        "driver_report_lag_frames=9 timing_query_interval=4 "
        "last_driver_report_present_id=2379\n",
    ]


def test_write_filtered_lines_folds_continuations_for_analysis(tmp_path) -> None:
    path = tmp_path / "capture.log"

    count = write_filtered_lines(
        [
            "2026-06-09 event=latency-layer-status status=create-swapchain "
            "live_swapchain_count=1 present_mode_name=IMMEDIATE "
            "swapchain_latency_mode=True\n",
            "2026-06-09 event=latency-layer-status status=latency-stream-stale "
            "live_swapchain_count=1\n",
            "swapchain_latency_mode=True driver_report_duplicate_count=240\n",
            "present_count=512 last_vulkan_present_id=512 "
            "latest_marker_present_id=512\n",
            "last_driver_report_present_id=470\n",
        ],
        path,
        sync=False,
    )

    assert count == 2
    analysis = write_analysis(path).read_text(encoding="utf-8")
    assert "root_cause=nvidia-reflex-timing-ring-stale" in analysis


def test_write_filtered_lines_replaces_existing_capture_by_default(tmp_path) -> None:
    path = tmp_path / "capture.log"
    path.write_text("old stale line\n", encoding="utf-8")

    count = write_filtered_lines(
        ["2026-06-09 event=latency-meter gpu-render-p95=16.00ms\n"],
        path,
        sync=False,
    )

    assert count == 1
    assert path.read_text(encoding="utf-8").splitlines() == [
        "2026-06-09 event=latency-meter gpu-render-p95=16.00ms",
    ]


def test_write_filtered_lines_can_append_existing_capture(tmp_path) -> None:
    path = tmp_path / "capture.log"
    path.write_text("old retained line\n", encoding="utf-8")

    count = write_filtered_lines(
        ["2026-06-09 event=latency-meter gpu-render-p95=16.00ms\n"],
        path,
        append=True,
        sync=False,
    )

    assert count == 1
    assert path.read_text(encoding="utf-8").splitlines() == [
        "old retained line",
        "2026-06-09 event=latency-meter gpu-render-p95=16.00ms",
    ]


def test_write_analysis_classifies_capture_file(tmp_path) -> None:
    capture = tmp_path / "capture.log"
    capture.write_text(
        "\n".join(
            [
                "2026-06-09 event=latency-layer-status "
                "status=create-swapchain live_swapchain_count=1 "
                "present_mode_name=IMMEDIATE swapchain_latency_mode=True",
                "2026-06-09 event=latency-layer-status "
                "status=latency-stream-stale live_swapchain_count=2 "
                "swapchain_latency_mode=True last_vulkan_present_id=512 "
                "latest_marker_present_id=512 last_driver_report_present_id=470 "
                "driver_report_duplicate_count=240",
            ]
        ),
        encoding="utf-8",
    )

    analysis = write_analysis(capture)

    assert analysis == tmp_path / "capture.log.analysis.txt"
    assert "root_cause=vkd3d-multi-swapchain-reflex-guard" in analysis.read_text(
        encoding="utf-8"
    )


def test_main_writes_analysis_after_keyboard_interrupt(
    tmp_path, monkeypatch, capsys
) -> None:
    capture = tmp_path / "capture.log"
    capture.write_text(
        "\n".join(
            [
                "2026-06-09 event=latency-layer-status "
                "status=create-swapchain live_swapchain_count=1 "
                "present_mode_name=IMMEDIATE swapchain_latency_mode=True",
                "2026-06-09 event=latency-layer-status "
                "status=latency-stream-stale live_swapchain_count=1 "
                "swapchain_latency_mode=True last_vulkan_present_id=512 "
                "latest_marker_present_id=512 last_driver_report_present_id=470 "
                "driver_report_duplicate_count=240",
            ]
        ),
        encoding="utf-8",
    )

    def raise_interrupt(*_args, **_kwargs) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(flow_capture, "capture_journal", raise_interrupt)

    assert flow_capture.main(["--output", str(capture)]) == 0

    output = capsys.readouterr().out
    assert f"capture={capture}" in output
    assert "captured_lines=2" in output
    assert "interrupted=True" in output
    assert "root_cause=nvidia-reflex-timing-ring-stale" in output
    assert capture.with_suffix(".log.analysis.txt").exists()


def test_main_replaces_capture_by_default(tmp_path, monkeypatch, capsys) -> None:
    capture = tmp_path / "capture.log"
    capture.write_text("old stale line\n", encoding="utf-8")
    seen: dict[str, object] = {}

    def fake_capture(output_path, **kwargs) -> int:
        seen.update(kwargs)
        output_path.write_text(
            "2026-06-09 event=latency-meter gpu-render-p95=16.00ms\n",
            encoding="utf-8",
        )
        return 1

    monkeypatch.setattr(flow_capture, "capture_journal", fake_capture)

    assert flow_capture.main(["--output", str(capture)]) == 0

    assert seen["append"] is False
    assert "old stale line" not in capture.read_text(encoding="utf-8")
    assert "captured_lines=1" in capsys.readouterr().out


def test_main_passes_until_bound(tmp_path, monkeypatch) -> None:
    capture = tmp_path / "capture.log"
    seen: dict[str, object] = {}

    def fake_capture(output_path, **kwargs) -> int:
        seen.update(kwargs)
        output_path.write_text(
            "2026-06-09 event=latency-meter gpu-render-p95=16.00ms\n",
            encoding="utf-8",
        )
        return 1

    monkeypatch.setattr(flow_capture, "capture_journal", fake_capture)

    assert (
        flow_capture.main(
            [
                "--output",
                str(capture),
                "--no-follow",
                "--since",
                "2026-06-09 21:45:00",
                "--until",
                "2026-06-09 21:46:30",
            ]
        )
        == 0
    )

    assert seen["follow"] is False
    assert seen["since"] == "2026-06-09 21:45:00"
    assert seen["until"] == "2026-06-09 21:46:30"


def test_main_can_append_capture(tmp_path, monkeypatch) -> None:
    capture = tmp_path / "capture.log"
    seen: dict[str, object] = {}

    def fake_capture(output_path, **kwargs) -> int:
        seen.update(kwargs)
        output_path.write_text(
            "2026-06-09 event=latency-meter gpu-render-p95=16.00ms\n",
            encoding="utf-8",
        )
        return 1

    monkeypatch.setattr(flow_capture, "capture_journal", fake_capture)

    assert flow_capture.main(["--output", str(capture), "--append"]) == 0

    assert seen["append"] is True
