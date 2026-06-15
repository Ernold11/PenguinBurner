"""Tests for Q2RTX launcher-error recovery: RUNPATH patching, same-mode retry,
and reclassification so a failed *launch* never blacklists a voltage."""

from __future__ import annotations

from pathlib import Path

from auto_uv.persistence.unsafe_voltage_cache import controlled_failure_reason
from auto_uv.q2rtx.probe_runtime_guardrails import (
    probe_failure_should_mark_voltage_unsafe,
)
from stability.q2rtx.constants import Q2RTX_LAUNCHER_ERROR_REASON
from stability.q2rtx.elf_runpath import ORIGIN_RUNPATH, patch_runpath_to_origin
from stability.q2rtx.models import Q2RTXStabilityResult
from stability.q2rtx.runtime import (
    _reclassify_launcher_failure,
    _result_has_launcher_error,
    _result_is_retryable_launcher_failure,
)


# ---- RUNPATH patcher: rewrite the vendored build path to $ORIGIN ----

# A stand-in for the q2rtx ELF: the vendored RUNPATH string surrounded by other
# NUL-terminated .dynstr entries, so we can assert neighbours stay intact.
_ELF_BLOB = b"\x7fELF" + b"\x00" * 16 + b"libssl.so.1.1\x00/mnt/q2rtx/.\x00libc.so.6\x00"


def test_patch_runpath_rewrites_build_path_to_origin(tmp_path: Path) -> None:
    binary = tmp_path / "q2rtx"
    binary.write_bytes(_ELF_BLOB)

    previous = patch_runpath_to_origin(binary)

    patched = binary.read_bytes()
    assert previous == "/mnt/q2rtx/."
    assert b"$ORIGIN\x00" in patched
    assert b"/mnt/q2rtx" not in patched
    assert len(patched) == len(_ELF_BLOB)  # in-place: no offsets shift
    assert b"libssl.so.1.1\x00" in patched and b"libc.so.6\x00" in patched


def test_patch_runpath_is_idempotent(tmp_path: Path) -> None:
    binary = tmp_path / "q2rtx"
    binary.write_bytes(_ELF_BLOB)

    assert patch_runpath_to_origin(binary) == "/mnt/q2rtx/."
    assert patch_runpath_to_origin(binary) == ORIGIN_RUNPATH


def test_patch_runpath_noop_when_build_path_absent(tmp_path: Path) -> None:
    binary = tmp_path / "other"
    original = b"a binary without the vendored runpath"
    binary.write_bytes(original)

    assert patch_runpath_to_origin(binary) is None
    assert binary.read_bytes() == original


# ---- same-mode retry / reclassification helpers ----


def _make_result(*, success: bool, reason: str, output_tail: list[str]):
    return Q2RTXStabilityResult(
        success=success,
        reason=reason,
        workload_kind="timedemo",
        workload_name="q2demo1",
        command=["q2rtx"],
        executable_path=Path("/tmp/q2rtx"),
        workdir=Path("/tmp"),
        duration_requested_s=450,
        timedemo_loops_requested=None,
        duration_observed_s=0.8,
        demo_path=None,
        log_path=Path("/tmp/q2rtx.log"),
        process_exit_code=-11,
        shutdown_mode="completed",
        fatal_output_matches=[],
        xid_messages=[],
        timedemo_runs=[],
        telemetry_samples=[],
        companion_telemetry_samples=[],
        output_tail=output_tail,
    )


_LIBSSL_TAIL = [
    "[gamescope] [Info]  vblank: Using timerfd.",
    "q2rtx: error while loading shared libraries: libssl.so.1.1: "
    "cannot open shared object file: No such file or directory",
    "[gamescope] [Info]  launch: Primary child shut down!",
]


def test_libssl_failure_is_a_retryable_launcher_error() -> None:
    result = _make_result(
        success=False, reason="timedemo-nonzero-exit", output_tail=_LIBSSL_TAIL
    )
    assert _result_has_launcher_error(result) is True
    assert _result_is_retryable_launcher_failure(result) is True


def test_reclassify_relabels_launcher_failure() -> None:
    result = _make_result(
        success=False, reason="timedemo-nonzero-exit", output_tail=_LIBSSL_TAIL
    )
    reclassified = _reclassify_launcher_failure(result)
    assert reclassified.reason == Q2RTX_LAUNCHER_ERROR_REASON


def test_successful_result_is_not_a_launcher_failure() -> None:
    result = _make_result(success=True, reason="ok", output_tail=["frames"])
    assert _result_has_launcher_error(result) is False
    assert _result_is_retryable_launcher_failure(result) is False
    assert _reclassify_launcher_failure(result) is result


def test_real_instability_is_not_a_launcher_error() -> None:
    result = _make_result(
        success=False,
        reason="timedemo-nonzero-exit",
        output_tail=["VK_ERROR_DEVICE_LOST", "device lost"],
    )
    assert _result_has_launcher_error(result) is False
    assert _result_is_retryable_launcher_failure(result) is False


# ---- the blacklist gate must skip a launcher error ----


def test_launcher_error_reason_never_blacklists_voltage() -> None:
    assert controlled_failure_reason(Q2RTX_LAUNCHER_ERROR_REASON) is True
    assert (
        probe_failure_should_mark_voltage_unsafe(Q2RTX_LAUNCHER_ERROR_REASON) is False
    )


def test_real_instability_reason_still_blacklists() -> None:
    assert probe_failure_should_mark_voltage_unsafe("timedemo-nonzero-exit") is True
