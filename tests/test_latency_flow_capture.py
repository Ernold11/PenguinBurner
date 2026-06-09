from datetime import datetime

import latency_telemetry.flow_capture as flow_capture
from latency_telemetry.flow_capture import (
    default_capture_path,
    is_latency_flow_line,
    write_analysis,
    write_filtered_lines,
)


def test_latency_flow_capture_filter_keeps_only_flow_lines() -> None:
    assert is_latency_flow_line("status=create-swapchain present_mode_name=IMMEDIATE")
    assert is_latency_flow_line("event=latency-raw present_id=12 gpu_render_us=16000")
    assert is_latency_flow_line("status=latency-stream-stale live_swapchain_count=1")
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
