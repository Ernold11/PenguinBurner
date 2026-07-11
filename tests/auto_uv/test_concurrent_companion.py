"""Tests for the concurrent CUDA companion reap logic."""

from __future__ import annotations

import subprocess
from pathlib import Path

from stability.q2rtx.runtime import _reap_concurrent_companion


def start(command: str) -> subprocess.Popen:
    return subprocess.Popen(["/bin/sh", "-c", command], start_new_session=True)


def test_clean_companion_exit_is_success(tmp_path: Path) -> None:
    log = tmp_path / "cuda.log"
    log.write_text("all good\n")
    companion = start("exit 0")
    exit_code, reason = _reap_concurrent_companion(
        companion,
        companion_log_path=log,
        q2rtx_succeeded=True,
        grace_s=10.0,
    )
    assert exit_code == 0
    assert reason is None


def test_nonzero_companion_exit_is_reported(tmp_path: Path) -> None:
    log = tmp_path / "cuda.log"
    log.write_text("all good\n")
    companion = start("exit 3")
    exit_code, reason = _reap_concurrent_companion(
        companion,
        companion_log_path=log,
        q2rtx_succeeded=True,
        grace_s=10.0,
    )
    assert exit_code == 3
    assert reason is None


def test_fatal_pattern_in_companion_log_is_fatal_cuda_output(tmp_path: Path) -> None:
    log = tmp_path / "cuda.log"
    log.write_text("CUDA kernel: Segmentation fault\n")
    companion = start("exit 0")
    exit_code, reason = _reap_concurrent_companion(
        companion,
        companion_log_path=log,
        q2rtx_succeeded=True,
        grace_s=10.0,
    )
    assert exit_code == 0
    assert reason == "fatal-cuda-output"


def test_straggler_killed_at_grace_is_not_a_failure(tmp_path: Path) -> None:
    log = tmp_path / "cuda.log"
    log.write_text("still running\n")
    companion = start("sleep 60")
    exit_code, reason = _reap_concurrent_companion(
        companion,
        companion_log_path=log,
        q2rtx_succeeded=True,
        grace_s=0.3,
    )
    assert exit_code is None
    assert reason is None
    assert companion.poll() is not None, "companion must be terminated"


def test_failed_benchmark_kills_companion_without_blame(tmp_path: Path) -> None:
    log = tmp_path / "cuda.log"
    log.write_text("irrelevant\n")
    companion = start("sleep 60")
    exit_code, reason = _reap_concurrent_companion(
        companion,
        companion_log_path=log,
        q2rtx_succeeded=False,
        grace_s=10.0,
    )
    assert reason is None
    assert companion.poll() is not None, "companion must be terminated"
    del exit_code
