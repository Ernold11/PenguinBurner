from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
import tarfile

import stability.q2rtx.runtime as q2rtx_runtime
from stability.q2rtx.install import (
    _copy_system_openssl_111_libs,
    _emit_dependency_progress,
    _extract_compat_openssl_rpm,
    _extract_q2rtx_archive,
    _progress_range_value,
    _require_https_url,
)
from stability.q2rtx.models import (
    Q2RTXStabilityConfig,
    Q2RTXStabilityResult,
    StabilityTestError,
    TimedemoRun,
)
from stability.q2rtx.output import (
    _format_live_progress_state,
    _scan_output_for_fatal_patterns,
)
from stability.q2rtx.reporting import _filter_report_output_tail
from stability.q2rtx.runtime import (
    _result_looks_like_gamescope_startup_crash,
    build_timedemo_command,
)
from stability.q2rtx.telemetry import _xid_message_is_at_or_after


def _write_tar(path: Path, members: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, payload in members.items():
            source = path.parent / name.replace("/", "_")
            source.write_bytes(payload)
            archive.add(source, arcname=name)


def test_q2rtx_download_urls_must_be_https() -> None:
    assert _require_https_url("https://example.test/file.tar.gz") == (
        "https://example.test/file.tar.gz"
    )

    for url in ["http://example.test/file.tar.gz", "file:///tmp/file.tar.gz"]:
        try:
            _require_https_url(url)
        except StabilityTestError as exc:
            assert "non-HTTPS" in str(exc)
        else:
            raise AssertionError(f"expected rejection for {url}")


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


def test_dependency_download_progress_maps_to_overall_range() -> None:
    assert _progress_range_value(10.0, 70.0, 0.0) == 10.0
    assert _progress_range_value(10.0, 70.0, 50.0) == 40.0
    assert _progress_range_value(10.0, 70.0, 100.0) == 70.0


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


def test_compat_openssl_accepts_rpm2cpio_tar_payload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    payload_tar = tmp_path / "payload.tar.gz"
    _write_tar(
        payload_tar,
        {
            "usr/lib64/libssl.so.1.1": b"ssl",
            "usr/lib64/libcrypto.so.1.1": b"crypto",
        },
    )
    fake_rpm2cpio = tmp_path / "rpm2cpio"
    fake_rpm2cpio.write_text(
        '#!/bin/sh\n/bin/cat "$PAYLOAD_TAR"\n',
        encoding="utf-8",
    )
    fake_rpm2cpio.chmod(0o755)
    fake_cpio = tmp_path / "cpio"
    fake_cpio.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    fake_cpio.chmod(0o755)
    fake_rpm = tmp_path / "compat-openssl11.rpm"
    fake_rpm.write_bytes(b"not used by fake rpm2cpio")

    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("PAYLOAD_TAR", str(payload_tar))

    lib_dir = _extract_compat_openssl_rpm(fake_rpm, tmp_path / "compat")

    assert (lib_dir / "libssl.so.1.1").read_bytes() == b"ssl"
    assert (lib_dir / "libcrypto.so.1.1").read_bytes() == b"crypto"


def test_compat_openssl_does_not_feed_compressed_payload_to_cpio(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_rpm2cpio = tmp_path / "rpm2cpio"
    fake_rpm2cpio.write_text(
        "#!/bin/sh\nprintf '\\050\\265\\057\\375not-a-raw-cpio'\n",
        encoding="utf-8",
    )
    fake_rpm2cpio.chmod(0o755)
    fake_cpio = tmp_path / "cpio"
    cpio_marker = tmp_path / "cpio-was-called"
    fake_cpio.write_text(
        f"#!/bin/sh\ntouch {cpio_marker}\nexit 0\n",
        encoding="utf-8",
    )
    fake_cpio.chmod(0o755)
    fake_rpm = tmp_path / "compat-openssl11.rpm"
    fake_rpm.write_bytes(b"not used by fake rpm2cpio")

    monkeypatch.setenv("PATH", str(tmp_path))

    try:
        _extract_compat_openssl_rpm(fake_rpm, tmp_path / "compat")
    except StabilityTestError as exc:
        assert "zstd-compressed payload" in str(exc)
    else:
        raise AssertionError("expected compressed non-cpio payload rejection")

    assert not cpio_marker.exists()


def test_compat_openssl_accepts_zstd_compressed_rpm2cpio_payload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    payload_tar = tmp_path / "payload.tar.gz"
    _write_tar(
        payload_tar,
        {
            "usr/lib64/libssl.so.1.1": b"ssl",
            "usr/lib64/libcrypto.so.1.1": b"crypto",
        },
    )
    fake_rpm2cpio = tmp_path / "rpm2cpio"
    fake_rpm2cpio.write_text(
        "#!/bin/sh\nprintf '\\050\\265\\057\\375compressed-cpio-placeholder'\n",
        encoding="utf-8",
    )
    fake_rpm2cpio.chmod(0o755)
    fake_zstd = tmp_path / "zstd"
    fake_zstd.write_text(
        '#!/bin/sh\n/bin/cat "$PAYLOAD_TAR"\n',
        encoding="utf-8",
    )
    fake_zstd.chmod(0o755)
    fake_rpm = tmp_path / "compat-openssl11.rpm"
    fake_rpm.write_bytes(b"not used by fake rpm2cpio")

    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("PAYLOAD_TAR", str(payload_tar))

    lib_dir = _extract_compat_openssl_rpm(fake_rpm, tmp_path / "compat")

    assert (lib_dir / "libssl.so.1.1").read_bytes() == b"ssl"
    assert (lib_dir / "libcrypto.so.1.1").read_bytes() == b"crypto"


def test_compat_openssl_tools_run_with_c_locale(
    tmp_path: Path,
    monkeypatch,
) -> None:
    env_marker = tmp_path / "locale-env"
    fake_rpm2cpio = tmp_path / "rpm2cpio"
    fake_rpm2cpio.write_text(
        f"#!/bin/sh\nprintf '%s/%s' \"$LC_ALL\" \"$LANG\" > {env_marker}\nprintf '\\050\\265\\057\\375'\n",
        encoding="utf-8",
    )
    fake_rpm2cpio.chmod(0o755)
    fake_rpm = tmp_path / "compat-openssl11.rpm"
    fake_rpm.write_bytes(b"not used by fake rpm2cpio")

    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    monkeypatch.setenv("LANG", "de_DE.UTF-8")

    try:
        _extract_compat_openssl_rpm(fake_rpm, tmp_path / "compat")
    except StabilityTestError:
        pass
    else:
        raise AssertionError("expected compressed non-cpio payload rejection")

    assert env_marker.read_text(encoding="utf-8") == "C/C"


def test_system_openssl_111_libs_can_be_found_with_ldconfig(
    tmp_path: Path,
    monkeypatch,
) -> None:
    system_lib = tmp_path / "multiarch"
    system_lib.mkdir()
    (system_lib / "libssl.so.1.1").write_bytes(b"ssl")
    (system_lib / "libcrypto.so.1.1").write_bytes(b"crypto")
    fake_ldconfig = tmp_path / "ldconfig"
    fake_ldconfig.write_text(
        "#!/bin/sh\n"
        f"printf 'libssl.so.1.1 (libc6,x86-64) => {system_lib / 'libssl.so.1.1'}\\n'\n"
        f"printf 'libcrypto.so.1.1 (libc6,x86-64) => {system_lib / 'libcrypto.so.1.1'}\\n'\n",
        encoding="utf-8",
    )
    fake_ldconfig.chmod(0o755)

    monkeypatch.setenv("PATH", str(tmp_path))

    lib_dir = _copy_system_openssl_111_libs(tmp_path / "compat")

    assert lib_dir is not None
    assert (lib_dir / "libssl.so.1.1").read_bytes() == b"ssl"
    assert (lib_dir / "libcrypto.so.1.1").read_bytes() == b"crypto"


def test_system_openssl_111_libs_can_seed_compat_dir(
    tmp_path: Path,
    monkeypatch,
) -> None:
    system_lib = tmp_path / "usr" / "lib"
    system_lib.mkdir(parents=True)
    (system_lib / "libssl.so.1.1").write_bytes(b"ssl")
    (system_lib / "libcrypto.so.1.1").write_bytes(b"crypto")

    monkeypatch.setattr(
        "stability.q2rtx.install.Path",
        lambda value="": system_lib if str(value) == "/usr/lib" else Path(value),
    )

    lib_dir = _copy_system_openssl_111_libs(tmp_path / "compat")

    assert lib_dir is not None
    assert (lib_dir / "libssl.so.1.1").read_bytes() == b"ssl"
    assert (lib_dir / "libcrypto.so.1.1").read_bytes() == b"crypto"


def test_timedemo_command_uses_requested_resolution_and_run_count(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "q2rtx"
    command = build_timedemo_command(
        executable,
        demo_name="demo1",
        width=2560,
        height=1440,
        hide_window=True,
        timedemo_runs=3,
    )

    assert command[0] == str(executable)
    geometry = command[command.index("vid_geometry") + 1]
    assert geometry.startswith("2560x1440")
    assert command[command.index("drs_enable") + 1] == "0"
    assert command[command.index("drs_minscale") + 1] == "100"
    assert command[command.index("drs_maxscale") + 1] == "100"
    assert command[command.index("flt_fsr_enable") + 1] == "0"
    assert command[command.index("pt_num_bounce_rays") + 1] == "2"
    assert command[command.index("pt_reflect_refract") + 1] == "8"
    assert command[command.index("timedemo") + 1] == "3"
    assert command[-2:] == ["+demo", "demo1"]


def test_hidden_window_env_forces_offscreen_x11_without_display() -> None:
    hidden_env = q2rtx_runtime._apply_hidden_window_env(
        {"WAYLAND_DISPLAY": "wayland-0"},
        hide_window=True,
        use_headless_gamescope=False,
    )

    assert hidden_env["SDL_VIDEODRIVER"] == "x11"
    assert hidden_env["SDL_VIDEO_WINDOW_POS"] == "32000,32000"
    assert hidden_env["SDL_VIDEO_X11_FORCE_OVERRIDE_REDIRECT"] == "1"


def test_duration_based_timedemo_uses_calibrated_complete_loop_count(
    tmp_path: Path,
    monkeypatch,
) -> None:
    process_calls = []

    def fake_run_timedemo_process(**kwargs):
        process_calls.append(kwargs)
        requested_runs = int(kwargs["requested_runs"])
        return 0, float(requested_runs) * 10.0, [], [], "completed"

    def fake_extract_timedemo_runs(_log_path: Path):
        calibration = [TimedemoRun(run_index=1, frames=631, seconds=10.0, fps=63.1)]
        if len(process_calls) <= 1:
            return calibration
        return calibration + [
            TimedemoRun(run_index=index + 2, frames=631, seconds=10.0, fps=63.1)
            for index in range(3)
        ]

    monkeypatch.setattr(
        q2rtx_runtime,
        "_run_timedemo_process",
        fake_run_timedemo_process,
    )
    monkeypatch.setattr(
        q2rtx_runtime,
        "_extract_timedemo_runs",
        fake_extract_timedemo_runs,
    )
    monkeypatch.setattr(
        q2rtx_runtime,
        "_scan_output_for_fatal_patterns",
        lambda _path: [],
    )
    monkeypatch.setattr(
        q2rtx_runtime,
        "_query_xid_messages_since",
        lambda _start: [],
    )
    monkeypatch.setattr(q2rtx_runtime, "_read_recent_output", lambda _path: [])

    result = q2rtx_runtime._run_timedemo_session(
        config=Q2RTXStabilityConfig(
            duration_s=25,
            timedemo_loops=None,
            log_dir=tmp_path,
        ),
        executable_path=tmp_path / "q2rtx",
        workdir=tmp_path,
        workload_name="q2demo1",
        demo_path=None,
        log_path=tmp_path / "q2rtx.log",
        runtime_env={},
    )

    assert [call["requested_runs"] for call in process_calls] == [1, 3]
    assert process_calls[1]["section_name"] == "timedemo-loop x3"
    assert result.success is True
    assert result.reason == "ok"
    assert result.timedemo_loops_requested is None
    assert result.duration_requested_s == 25
    assert len(result.timedemo_runs) == 3


def test_gamescope_startup_crash_is_detected_for_fallback(tmp_path: Path) -> None:
    result = Q2RTXStabilityResult(
        success=False,
        reason="timedemo-metrics-missing",
        workload_kind="timedemo",
        workload_name="q2demo1",
        command=["q2rtx"],
        executable_path=tmp_path / "q2rtx",
        workdir=tmp_path,
        duration_requested_s=30,
        timedemo_loops_requested=3,
        duration_observed_s=2.0,
        demo_path=None,
        log_path=tmp_path / "q2rtx.log",
        process_exit_code=139,
        shutdown_mode="completed",
        fatal_output_matches=[],
        xid_messages=[],
        timedemo_runs=[],
        telemetry_samples=[],
        companion_telemetry_samples=[],
        output_tail=[
            "[Gamescope WSI] Creating swapchain",
            "Segmentation fault",
            "[gamescope] [Info]  launch: Primary child shut down!",
        ],
    )

    assert _result_looks_like_gamescope_startup_crash(result) is True


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


def test_timedemo_abort_policy_only_kills_immediate_failures() -> None:
    assert q2rtx_runtime._timedemo_abort_is_immediate("user-stop-requested") is True
    assert q2rtx_runtime._timedemo_abort_is_immediate("fatal-q2rtx-output") is True
    assert (
        q2rtx_runtime._timedemo_abort_is_immediate(
            "profile-verification-voltage-mismatch current=1025mV target=870mV"
        )
        is True
    )
    assert (
        q2rtx_runtime._timedemo_abort_is_immediate(
            "telemetry-live-load-lost current=45.0W"
        )
        is True
    )
    assert (
        q2rtx_runtime._timedemo_abort_is_immediate(
            "timedemo-live-frame-count current=0 expected=631"
        )
        is True
    )
    assert (
        q2rtx_runtime._timedemo_abort_is_immediate("timedemo-live-stall idle=20.0s")
        is True
    )
    assert (
        q2rtx_runtime._timedemo_abort_is_immediate(
            "telemetry-live-core_clock current=2400.0MHz"
        )
        is False
    )
    assert (
        q2rtx_runtime._timedemo_abort_is_immediate(
            "timedemo-live-fps-regression current=75.0"
        )
        is False
    )


def test_device_lost_output_is_fatal_case_insensitive(tmp_path: Path) -> None:
    log_path = tmp_path / "q2rtx.log"
    log_path.write_text("vk: Device lost!\n", encoding="utf-8")

    matches = _scan_output_for_fatal_patterns(log_path)

    assert "device lost" in matches


def test_report_output_tail_filters_normal_gamescope_shutdown_noise() -> None:
    assert _filter_report_output_tail(
        [
            "631 frames, 3.84 seconds: 164.365707 fps",
            "[Gamescope WSI] Destroying swapchain: 0x2dbc6790",
            "[Gamescope WSI] Destroyed swapchain: 0x2dbc6790",
            "Closing console log.",
            "[gamescope] [Info]  launch: Primary child shut down!",
            "(EE) failed to read Wayland events: Broken pipe",
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
