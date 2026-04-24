from __future__ import annotations

from pathlib import Path
import tarfile

from stability.q2rtx.install import _extract_q2rtx_archive, _require_https_url
from stability.q2rtx.models import Q2RTXStabilityResult, StabilityTestError
from stability.q2rtx.runtime import (
    _result_looks_like_gamescope_startup_crash,
    build_timedemo_command,
)


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
