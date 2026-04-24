from __future__ import annotations

from pathlib import Path
import tarfile

from stability.q2rtx.install import _extract_q2rtx_archive, _require_https_url
from stability.q2rtx.models import StabilityTestError
from stability.q2rtx.runtime import build_timedemo_command


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
