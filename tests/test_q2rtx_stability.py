from __future__ import annotations

from datetime import datetime
from pathlib import Path
import tarfile

from stability.q2rtx.install import (
    _copy_system_openssl_111_libs,
    _extract_compat_openssl_rpm,
    _extract_q2rtx_archive,
    _require_https_url,
)
from stability.q2rtx.models import Q2RTXStabilityResult, StabilityTestError
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
        "#!/bin/sh\n/bin/cat \"$PAYLOAD_TAR\"\n",
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
