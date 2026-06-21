from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
import tarfile
from types import SimpleNamespace

import pytest

import auto_uv.stability.q2rtx.downloader as q2rtx_downloader
import auto_uv.stability.q2rtx.gpu_binding as q2rtx_gpu_binding
import auto_uv.stability.q2rtx.install as q2rtx_install
import auto_uv.stability.q2rtx.runtime as q2rtx_runtime
import runtime.stability_test.q2rtx_cuda_workload_config as q2rtx_workload_config
from auto_uv.stability.q2rtx.constants import (
    PB_Q2RTX_ASSET_PREFIX,
    PB_Q2RTX_ASSET_SUFFIX,
    Q2RTX_REQUIRED_DATA_FILES,
)
from auto_uv.stability.q2rtx.install import (
    clear_q2rtx_stability_logs,
    _download_file_from_urls,
    _emit_dependency_progress,
    _extract_q2rtx_archive,
    _extract_q2rtx_data_files,
    _q2rtx_release_asset_urls,
    fetch_latest_q2rtx_release_metadata,
)
from auto_uv.stability.q2rtx.progress import _progress_range_value
from auto_uv.stability.q2rtx.models import (
    Q2RTXBenchmarkSummary,
    Q2RTXStabilityConfig,
    Q2RTXStabilityResult,
    StabilityTestError,
)
from auto_uv.stability.q2rtx.output import (
    _benchmark_summary_from_events,
    _format_live_progress_state,
    _scan_output_for_fatal_patterns,
)
from auto_uv.stability.q2rtx.reporting import _filter_report_output_tail
from auto_uv.stability.q2rtx.runtime import build_benchmark_command
from auto_uv.stability.q2rtx.telemetry import _xid_message_is_at_or_after


def _write_tar(path: Path, members: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, payload in members.items():
            source = path.parent / name.replace("/", "_")
            source.write_bytes(payload)
            archive.add(source, arcname=name)


def test_dependency_progress_payload_is_clamped_and_labeled() -> None:
    events = []

    _emit_dependency_progress(events.append, 123.4, "Ready", path="/tmp/q2rtx")

    assert events == [
        {
            "label": "Downloading dependencies",
            "percent": 100.0,
            "detail": "Ready",
            "path": "/tmp/q2rtx",
        }
    ]


def test_clear_q2rtx_stability_logs_removes_only_generated_logs(tmp_path: Path) -> None:
    old_log = tmp_path / "q2rtx-stability-20260620-123456.log"
    other_log = tmp_path / "other.log"
    nested_log = tmp_path / "q2rtx-stability-nested.log"
    old_log.write_text("old q2rtx output\n", encoding="utf-8")
    other_log.write_text("keep\n", encoding="utf-8")
    nested_log.mkdir()

    removed = clear_q2rtx_stability_logs(tmp_path, show_progress=False)

    assert removed == 1
    assert not old_log.exists()
    assert other_log.read_text(encoding="utf-8") == "keep\n"
    assert nested_log.is_dir()


def test_dependency_download_progress_maps_to_overall_range() -> None:
    assert _progress_range_value(10.0, 70.0, 0.0) == 10.0
    assert _progress_range_value(10.0, 70.0, 50.0) == 40.0
    assert _progress_range_value(10.0, 70.0, 100.0) == 70.0


def test_download_file_from_urls_retries_next_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    destination = tmp_path / "payload.tar.gz"
    calls: list[str] = []

    def fake_download_file(url: str, destination: Path, **_kwargs) -> None:
        calls.append(url)
        if "primary.example" in url:
            raise StabilityTestError("read timeout")
        destination.write_bytes(b"ok")

    monkeypatch.setattr(q2rtx_downloader, "_download_file", fake_download_file)

    selected_url = _download_file_from_urls(
        (
            "https://primary.example/payload.tar.gz",
            "https://mirror.example/payload.tar.gz",
        ),
        destination,
        label="test payload",
        show_progress=False,
    )

    assert selected_url == "https://mirror.example/payload.tar.gz"
    assert calls == [
        "https://primary.example/payload.tar.gz",
        "https://mirror.example/payload.tar.gz",
    ]
    assert destination.read_bytes() == b"ok"


def test_q2rtx_release_metadata_uses_latest_penguinburner_binary(monkeypatch) -> None:
    tag_name = "pb-benchmark-v0.1.1"
    asset_name = f"{PB_Q2RTX_ASSET_PREFIX}{tag_name}{PB_Q2RTX_ASSET_SUFFIX}"
    asset_url = (
        "https://github.com/jpietek/Q2RTX-headless/releases/download/"
        f"{tag_name}/{asset_name}"
    )

    monkeypatch.setattr(
        q2rtx_install,
        "_github_json",
        lambda _url: {
            "tag_name": tag_name,
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "name": f"{asset_name}.sha256",
                    "browser_download_url": f"{asset_url}.sha256",
                },
                {
                    "name": asset_name,
                    "browser_download_url": asset_url,
                },
            ],
        },
    )

    tag_name, asset_name, asset_url = fetch_latest_q2rtx_release_metadata()

    assert tag_name == "pb-benchmark-v0.1.1"
    assert asset_name == "q2rtx-penguinburner-pb-benchmark-v0.1.1-linux-x86_64.tar.gz"
    assert (
        asset_url
        == "https://github.com/jpietek/Q2RTX-headless/releases/download/"
        "pb-benchmark-v0.1.1/"
        "q2rtx-penguinburner-pb-benchmark-v0.1.1-linux-x86_64.tar.gz"
    )


def test_q2rtx_asset_urls_use_latest_primary_asset() -> None:
    tag_name = "pb-benchmark-v0.1.1"
    asset_name = f"{PB_Q2RTX_ASSET_PREFIX}{tag_name}{PB_Q2RTX_ASSET_SUFFIX}"
    asset_url = (
        "https://github.com/jpietek/Q2RTX-headless/releases/download/"
        f"{tag_name}/{asset_name}"
    )
    urls = _q2rtx_release_asset_urls(
        tag_name,
        asset_name,
        asset_url,
    )

    assert urls == (asset_url,)


def test_managed_q2rtx_source_version_reads_version_dir(
    tmp_path: Path,
    monkeypatch,
) -> None:
    managed_root = tmp_path / "q2rtx"
    version_dir = managed_root / "pb-benchmark-v0.1.0"
    binary = version_dir / "q2rtx"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"")

    monkeypatch.setattr(
        q2rtx_workload_config,
        "default_q2rtx_install_data_dir",
        lambda: managed_root,
    )

    assert (
        q2rtx_workload_config._managed_q2rtx_source_version(str(version_dir))
        == "pb-benchmark-v0.1.0"
    )
    assert (
        q2rtx_workload_config._managed_q2rtx_source_version(str(binary))
        == "pb-benchmark-v0.1.0"
    )
    assert q2rtx_workload_config._managed_q2rtx_source_version("/opt/q2rtx") is None


def test_refresh_stale_managed_q2rtx_installs_latest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    managed_root = tmp_path / "q2rtx"
    old_dir = managed_root / "pb-benchmark-v0.1.0"
    latest_dir = managed_root / "pb-benchmark-v0.1.1"
    calls = []

    monkeypatch.setattr(
        q2rtx_workload_config,
        "default_q2rtx_install_data_dir",
        lambda: managed_root,
    )
    monkeypatch.setattr(
        q2rtx_workload_config,
        "fetch_latest_q2rtx_release_metadata",
        lambda: ("pb-benchmark-v0.1.1", "asset.tar.gz", "https://example.test/asset"),
    )

    def fake_install_latest_q2rtx(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(version="pb-benchmark-v0.1.1", install_dir=latest_dir)

    monkeypatch.setattr(
        q2rtx_workload_config,
        "install_latest_q2rtx",
        fake_install_latest_q2rtx,
    )
    progress_events = []

    q2rtx_dir = q2rtx_workload_config._refresh_stale_managed_q2rtx_source(
        "Q2RTX stability",
        q2rtx_dir=str(old_dir),
        dependency_text_progress=False,
        dependency_progress_callback=None,
        emit_dependency_progress=lambda *args, **kwargs: progress_events.append(
            (args, kwargs)
        ),
    )

    assert q2rtx_dir == str(latest_dir)
    assert calls == [
        {
            "show_progress": False,
            "progress_callback": None,
        }
    ]
    assert progress_events


def test_q2rtx_archive_extracts_regular_payload(tmp_path: Path) -> None:
    archive_path = tmp_path / "q2rtx.tar.gz"
    install_dir = tmp_path / "install"
    _write_tar(
        archive_path,
        {
            "q2rtx/q2rtx.sh": b"#!/bin/sh\n",
            "q2rtx/baseq2/demo.dm2": b"demo",
        },
    )

    _extract_q2rtx_archive(archive_path, install_dir)

    assert (install_dir / "q2rtx.sh").read_bytes() == b"#!/bin/sh\n"
    assert (install_dir / "baseq2" / "demo.dm2").read_bytes() == b"demo"


def test_q2rtx_archive_rejects_path_traversal(tmp_path: Path) -> None:
    archive_path = tmp_path / "bad.tar.gz"
    install_dir = tmp_path / "install"
    _write_tar(archive_path, {"../escape.txt": b"bad"})

    try:
        _extract_q2rtx_archive(archive_path, install_dir)
    except StabilityTestError as exc:
        assert "suspicious archive member" in str(exc)
    else:
        raise AssertionError("expected path traversal rejection")

    assert not (tmp_path / "escape.txt").exists()


def test_q2rtx_data_extracts_only_required_shareware_files(tmp_path: Path) -> None:
    archive_path = tmp_path / "q2rtx-data.tar.gz"
    install_dir = tmp_path / "install"
    _write_tar(
        archive_path,
        {
            "q2rtx/baseq2/pak0.pak": b"pak",
            "q2rtx/baseq2/blue_noise.pkz": b"blue",
            "q2rtx/baseq2/q2rtx_media.pkz": b"media",
            "q2rtx/baseq2/shaders.pkz": b"shaders",
            "q2rtx/q2rtx": b"official binary ignored",
            "q2rtx/official-helper.so": b"helper ignored",
        },
    )

    _extract_q2rtx_data_files(
        archive_path,
        install_dir,
        required_files=Q2RTX_REQUIRED_DATA_FILES,
    )

    assert (install_dir / "baseq2" / "pak0.pak").read_bytes() == b"pak"
    assert (install_dir / "baseq2" / "blue_noise.pkz").read_bytes() == b"blue"
    assert (install_dir / "baseq2" / "q2rtx_media.pkz").read_bytes() == b"media"
    assert (install_dir / "baseq2" / "shaders.pkz").read_bytes() == b"shaders"
    assert not (install_dir / "q2rtx").exists()
    assert not (install_dir / "official-helper.so").exists()


def test_q2rtx_data_extraction_requires_all_shareware_files(tmp_path: Path) -> None:
    archive_path = tmp_path / "q2rtx-data.tar.gz"
    install_dir = tmp_path / "install"
    _write_tar(archive_path, {"q2rtx/baseq2/pak0.pak": b"pak"})
    try:
        _extract_q2rtx_data_files(
            archive_path,
            install_dir,
            required_files=Q2RTX_REQUIRED_DATA_FILES,
        )
    except StabilityTestError as exc:
        assert "missing required files" in str(exc)
    else:
        raise AssertionError("expected missing shareware data rejection")


def test_benchmark_command_uses_requested_resolution(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "q2rtx"
    command = build_benchmark_command(
        executable,
        demo_name="demo1",
        width=2560,
        height=1440,
    )

    assert command[0] == str(executable)
    assert command[command.index("bench") + 1] == "1"
    assert command[command.index("bench_demo") + 1] == "demo1"
    assert command[command.index("bench_headless") + 1] == "1"
    assert command[command.index("bench_width") + 1] == "2560"
    assert command[command.index("bench_height") + 1] == "1440"
    assert command[command.index("bench_min_loops") + 1] == "1"
    assert command[command.index("bench_constant_load") + 1] == "1"
    assert command[command.index("bench_event_stdout") + 1] == "1"
    assert command[command.index("drs_enable") + 1] == "0"
    assert command[command.index("drs_minscale") + 1] == "100"
    assert command[command.index("drs_maxscale") + 1] == "100"
    assert command[command.index("flt_fsr_enable") + 1] == "0"
    assert command[command.index("pt_num_bounce_rays") + 1] == "2"
    assert command[command.index("pt_reflect_refract") + 1] == "8"


def test_benchmark_command_event_fd_disables_stdout_events(tmp_path: Path) -> None:
    executable = tmp_path / "q2rtx"
    command = build_benchmark_command(
        executable,
        demo_name="demo1",
        width=3840,
        height=2160,
    )

    command_with_fd = q2rtx_runtime._q2rtx_command_with_event_fd(command, 42)

    assert command_with_fd[command_with_fd.index("bench_event_stdout") + 1] == "0"
    assert command_with_fd[command_with_fd.index("bench_event_fd") + 1] == "42"


def test_benchmark_events_parse_hot_run_summary() -> None:
    events = [
        {"event": "start"},
        {
            "event": "phase",
            "name": "measure_start",
            "elapsed_ms": 1234.0,
        },
        {
            "event": "loop_done",
            "loop": 1,
            "frames": 631,
            "duration_ms": 25000.0,
            "fps": 25.24,
        },
        {
            "event": "done",
            "reason": "target",
            "loops": 1,
            "loops_started": 2,
            "demo_frames": 631,
            "render_frames": 756,
            "target_ms": 30000.0,
            "measured_ms": 30000.0,
            "render_ms": 29900.0,
            "drain_ms": 100.0,
            "loop_fps_mean": 25.24,
            "fps_avg": 25.2,
            "fps_min": 20.0,
            "fps_max": 30.0,
            "fps_mean": 25.1,
            "frame_ms_min": 33.0,
            "frame_ms_max": 50.0,
            "frame_ms_mean": 39.6,
        },
    ]

    summary = _benchmark_summary_from_events(events)

    assert summary is not None
    assert summary.measure_start_elapsed_s == pytest.approx(1.234)
    assert summary.measured_s == pytest.approx(30.0)
    assert summary.render_frames == 756
    assert summary.fps_avg == pytest.approx(25.2)


def test_q2rtx_runtime_env_forces_nvidia_prime_vulkan_offload() -> None:
    env = q2rtx_runtime._apply_nvidia_render_offload_env(
        {"__VK_LAYER_NV_optimus": "non_NVIDIA_only"},
        selected_gpu={},
    )

    assert env["__NV_PRIME_RENDER_OFFLOAD"] == "1"
    assert env["__VK_LAYER_NV_optimus"] == "NVIDIA_only"
    assert env["__GLX_VENDOR_LIBRARY_NAME"] == "nvidia"


def test_q2rtx_runtime_env_binds_selected_gpu_from_runtime_identity() -> None:
    selected_gpu = {
        "dri_prime": "pci-0000_03_00_0",
        "vk_loader_device_select": "0x10de:0x2c02",
        "mesa_vk_device_select": "10de:2c02",
    }

    env = q2rtx_runtime._apply_nvidia_render_offload_env(
        {},
        selected_gpu=selected_gpu,
    )

    assert env["DRI_PRIME"] == "pci-0000_03_00_0!"
    assert env["VK_LOADER_DEVICE_SELECT"] == "0x10de:0x2c02"
    assert env["MESA_VK_DEVICE_SELECT"] == "10de:2c02!"
    assert env["MESA_VK_DEVICE_SELECT_FORCE_DEFAULT_DEVICE"] == "1"


def test_q2rtx_selected_gpu_identity_is_derived_from_nvml(monkeypatch) -> None:
    monkeypatch.setattr(
        q2rtx_gpu_binding,
        "query_nvml_gpu_identity",
        lambda gpu_index: SimpleNamespace(
            index=int(gpu_index),
            name="NVIDIA GeForce RTX 5090",
            pci_bus_id="00000000:03:00.0",
            pci_device_id="0x2C0210DE",
            uuid="GPU-test",
        ),
    )

    selected_gpu = q2rtx_gpu_binding._query_selected_nvidia_gpu(1)

    assert selected_gpu["index"] == "1"
    assert selected_gpu["pci_bus_id"] == "00000000:03:00.0"
    assert selected_gpu["dri_prime"] == "pci-0000_03_00_0"
    assert selected_gpu["vk_loader_device_select"] == "0x10de:0x2c02"
    assert selected_gpu["mesa_vk_device_select"] == "10de:2c02"


def test_duration_based_benchmark_uses_single_exact_duration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    process_calls = []
    summary = Q2RTXBenchmarkSummary(
        reason="target",
        loops=1,
        loops_started=1,
        demo_frames=631,
        render_frames=631,
        target_s=25.0,
        measured_s=25.0,
        render_s=25.0,
        drain_s=0.0,
        loop_fps_mean=25.24,
        fps_avg=25.24,
        fps_min=24.0,
        fps_max=26.0,
        fps_mean=25.24,
        frame_ms_min=38.0,
        frame_ms_max=42.0,
        frame_ms_mean=39.6,
        measure_start_elapsed_s=1.0,
    )

    def fake_run_benchmark_process(**kwargs):
        process_calls.append(kwargs)
        return q2rtx_runtime._Q2RTXProcessRun(
            process_exit_code=0,
            observed_duration_s=26.0,
            telemetry_samples=[],
            companion_telemetry_samples=[],
            exit_reason="completed",
            benchmark_events=[
                {"event": "start"},
                {"event": "phase", "name": "measure_start", "elapsed_ms": 1000.0},
                {"event": "done"},
            ],
            benchmark_event_errors=[],
            benchmark_summary=summary,
            benchmark_telemetry_samples=[],
            fatal_output_matches=[],
            output_tail=[],
        )

    monkeypatch.setattr(
        q2rtx_runtime,
        "_run_benchmark_process",
        fake_run_benchmark_process,
    )
    monkeypatch.setattr(
        q2rtx_runtime,
        "_query_xid_messages_since",
        lambda _start: [],
    )

    result = q2rtx_runtime._run_benchmark_session(
        config=Q2RTXStabilityConfig(
            duration_s=25,
            log_dir=tmp_path,
        ),
        executable_path=tmp_path / "q2rtx",
        workdir=tmp_path,
        workload_name="q2demo1",
        demo_path=None,
        log_path=tmp_path / "q2rtx.log",
        runtime_env={},
    )

    assert process_calls[0]["section_name"] == "benchmark 25.0s"
    assert result.success is True
    assert result.reason == "ok"
    assert result.duration_requested_s == 25
    assert result.workload_kind == "benchmark"
    assert result.benchmark_summary is summary
    assert result.benchmark_measure_start_s == pytest.approx(1.0)
    assert result.benchmark_summary.fps_avg == pytest.approx(25.24)


def test_benchmark_fails_when_measure_start_is_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    summary = Q2RTXBenchmarkSummary(
        reason="target",
        loops=1,
        loops_started=1,
        demo_frames=631,
        render_frames=631,
        target_s=30.0,
        measured_s=30.0,
        render_s=30.0,
        drain_s=0.0,
        loop_fps_mean=21.0,
        fps_avg=21.0,
        fps_min=20.0,
        fps_max=22.0,
        fps_mean=21.0,
        frame_ms_min=45.0,
        frame_ms_max=50.0,
        frame_ms_mean=47.6,
        measure_start_elapsed_s=None,
    )

    def fake_run_benchmark_process(**_kwargs):
        return q2rtx_runtime._Q2RTXProcessRun(
            process_exit_code=0,
            observed_duration_s=30.0,
            telemetry_samples=[],
            companion_telemetry_samples=[],
            exit_reason="completed",
            benchmark_events=[
                {"event": "start"},
                {"event": "done"},
            ],
            benchmark_event_errors=[],
            benchmark_summary=summary,
            benchmark_telemetry_samples=[],
            fatal_output_matches=[],
            output_tail=[],
        )

    monkeypatch.setattr(
        q2rtx_runtime,
        "_run_benchmark_process",
        fake_run_benchmark_process,
    )
    monkeypatch.setattr(
        q2rtx_runtime,
        "_query_xid_messages_since",
        lambda _start: [],
    )

    result = q2rtx_runtime._run_benchmark_session(
        config=Q2RTXStabilityConfig(duration_s=30, log_dir=tmp_path),
        executable_path=tmp_path / "q2rtx",
        workdir=tmp_path,
        workload_name="q2demo1",
        demo_path=None,
        log_path=tmp_path / "q2rtx.log",
        runtime_env={},
    )

    assert result.success is False
    assert result.reason == "benchmark-measure-start-missing"


def test_cuda_companion_abort_preserves_abort_reason(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class DummyVoltageSession:
        def __init__(self, gpu_index: int) -> None:
            self.gpu_index = int(gpu_index)

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        q2rtx_runtime,
        "_HiddenNvmlVoltageSession",
        DummyVoltageSession,
    )
    monkeypatch.setattr(q2rtx_runtime, "query_gpu_metrics", lambda *args, **kwargs: None)
    monkeypatch.setattr(q2rtx_runtime, "_query_xid_messages_since", lambda _start: [])

    result = q2rtx_runtime.run_cuda_stability_test(
        Q2RTXStabilityConfig(
            duration_s=1,
            log_dir=tmp_path,
            poll_interval_s=0.01,
            companion_command=(
                sys.executable,
                "-c",
                "import time; time.sleep(30)",
            ),
            abort_callback=lambda _state: "user-stop-requested",
        )
    )

    assert result.success is False
    assert result.reason == "user-stop-requested"
    assert result.shutdown_mode == "user-stop-requested"
    assert result.process_exit_code == -15


def test_cuda_companion_progress_is_labeled_as_cuda() -> None:
    line = _format_live_progress_state(
        {
            "workload_name": "q2demo1",
            "running": "cuda",
            "elapsed_s": 512.0,
        },
        prefix="Stability live:",
    )

    assert "workload=cuda-compute" in line
    assert "running=cuda" in line
    assert "demo=q2demo1" not in line
    assert "elapsed=512.0s" in line


def test_fatal_output_abort_reason_uses_active_workload() -> None:
    assert (
        q2rtx_runtime._fatal_output_abort_reason(["device lost"], running="q2rtx")
        == "fatal-q2rtx-output"
    )
    assert (
        q2rtx_runtime._fatal_output_abort_reason(["device lost"], running="cuda")
        == "fatal-cuda-output"
    )
    assert q2rtx_runtime._fatal_output_abort_reason([], running="q2rtx") is None


def test_cuda_stability_aborts_immediately_on_fatal_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class DummyVoltageSession:
        def __init__(self, gpu_index: int) -> None:
            self.gpu_index = int(gpu_index)

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        q2rtx_runtime,
        "_HiddenNvmlVoltageSession",
        DummyVoltageSession,
    )
    monkeypatch.setattr(q2rtx_runtime, "query_gpu_metrics", lambda *args, **kwargs: None)
    monkeypatch.setattr(q2rtx_runtime, "_query_xid_messages_since", lambda _start: [])
    monkeypatch.setattr(
        q2rtx_runtime,
        "_scan_output_for_fatal_patterns",
        lambda _path: ["device lost"],
    )

    result = q2rtx_runtime.run_cuda_stability_test(
        Q2RTXStabilityConfig(
            duration_s=30,
            log_dir=tmp_path,
            poll_interval_s=0.01,
            companion_command=(
                sys.executable,
                "-c",
                "import time; time.sleep(30)",
            ),
        )
    )

    assert result.success is False
    assert result.reason == "fatal-cuda-output"
    assert result.shutdown_mode == "fatal-cuda-output"
    assert result.process_exit_code == -15
    assert result.duration_observed_s < 5.0


def test_benchmark_abort_policy_only_kills_immediate_failures() -> None:
    assert q2rtx_runtime._benchmark_abort_is_immediate("user-stop-requested") is True
    assert q2rtx_runtime._benchmark_abort_is_immediate("fatal-q2rtx-output") is True
    assert (
        q2rtx_runtime._benchmark_abort_is_immediate(
            "profile-verification-voltage-mismatch current=1025mV target=870mV"
        )
        is True
    )
    assert (
        q2rtx_runtime._benchmark_abort_is_immediate(
            "telemetry-live-load-lost current=45.0W"
        )
        is True
    )
    assert (
        q2rtx_runtime._benchmark_abort_is_immediate(
            "q2rtx-selected-nvidia-gpu-idle max_util=0.0%"
        )
        is True
    )
    assert (
        q2rtx_runtime._benchmark_abort_is_immediate(
            "telemetry-live-core_clock current=2400.0MHz"
        )
        is False
    )


def test_device_lost_output_is_fatal_case_insensitive(tmp_path: Path) -> None:
    log_path = tmp_path / "q2rtx.log"
    log_path.write_text("vk: Device lost!\n", encoding="utf-8")

    matches = _scan_output_for_fatal_patterns(log_path)

    assert "device lost" in matches


def test_any_vulkan_error_output_is_fatal(tmp_path: Path) -> None:
    log_path = tmp_path / "q2rtx.log"
    log_path.write_text(
        "vkQueueSubmit failed: VK_ERROR_MEMORY_MAP_FAILED\n"
        "extension path failed: VK_ERROR_UNKNOWN_VENDOR_THING\n",
        encoding="utf-8",
    )

    matches = _scan_output_for_fatal_patterns(log_path)

    assert "VK_ERROR_MEMORY_MAP_FAILED" in matches
    assert "VK_ERROR_UNKNOWN_VENDOR_THING" in matches


def test_signal_exit_reason_is_explicit() -> None:
    assert (
        q2rtx_runtime._q2rtx_exit_failure_reason(-11)
        == "benchmark-crashed-signal-SIGSEGV"
    )
    assert q2rtx_runtime._q2rtx_exit_failure_reason(2) == "benchmark-nonzero-exit-2"


def test_loader_error_output_is_launcher_failure_not_fatal_output(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "q2rtx.log"
    log_path.write_text(
        "q2rtx: error while loading shared libraries: "
        "libvulkan.so.1: cannot open shared object file\n",
        encoding="utf-8",
    )

    matches = _scan_output_for_fatal_patterns(log_path)

    assert "error while loading shared libraries" not in matches


def test_loader_error_result_is_reclassified(tmp_path: Path) -> None:
    result = Q2RTXStabilityResult(
        success=False,
        reason="benchmark-nonzero-exit-127",
        workload_kind="benchmark",
        workload_name="q2demo1",
        command=["q2rtx"],
        executable_path=tmp_path / "q2rtx",
        workdir=tmp_path,
        duration_requested_s=30,
        duration_observed_s=0.8,
        demo_path=None,
        log_path=tmp_path / "q2rtx.log",
        process_exit_code=127,
        shutdown_mode="completed",
        fatal_output_matches=[],
        xid_messages=[],
        telemetry_samples=[],
        companion_telemetry_samples=[],
        output_tail=[
            "q2rtx: error while loading shared libraries: "
            "libvulkan.so.1: cannot open shared object file",
        ],
    )

    assert (
        q2rtx_runtime._reclassify_launcher_failure(result).reason
        == "q2rtx-launcher-error"
    )


def test_report_output_tail_filters_console_shutdown_noise() -> None:
    assert _filter_report_output_tail(
        [
            "631 frames, 3.84 seconds: 164.365707 fps",
            "Closing console log.",
            "Segmentation fault",
        ]
    ) == [
        "631 frames, 3.84 seconds: 164.365707 fps",
        "Segmentation fault",
    ]


def test_xid_timestamp_filter_ignores_old_short_iso_journal_lines() -> None:
    started_at = datetime.fromisoformat("2026-04-25T20:38:28+02:00")

    old_line = "2026-04-25T20:34:24+02:00 home kernel: NVRM: Xid (PCI:0000:2b:00): 109"
    current_line = (
        "2026-04-25T20:38:29+02:00 home kernel: NVRM: Xid (PCI:0000:2b:00): 109"
    )

    assert _xid_message_is_at_or_after(old_line, started_at) is False
    assert _xid_message_is_at_or_after(current_line, started_at) is True
