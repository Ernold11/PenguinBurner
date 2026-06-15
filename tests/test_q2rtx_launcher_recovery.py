"""Tests for Q2RTX launcher-error recovery: RUNPATH patching, same-mode retry,
and reclassification so a failed *launch* never blacklists a voltage."""

from __future__ import annotations

from pathlib import Path
import struct

from auto_uv.persistence.unsafe_voltage_cache import controlled_failure_reason
from auto_uv.q2rtx.probe_runtime_guardrails import (
    probe_failure_should_mark_voltage_unsafe,
)
from stability.q2rtx.constants import Q2RTX_LAUNCHER_ERROR_REASON
from stability.q2rtx.elf_runpath import (
    ORIGIN_RUNPATH,
    _find_runpath_string,
    patch_runpath_to_origin,
)
from stability.q2rtx.models import Q2RTXStabilityResult
from stability.q2rtx.runtime import (
    _reclassify_launcher_failure,
    _result_has_launcher_error,
    _result_is_retryable_launcher_failure,
)


# ---- minimal ELF builder (64-bit LE, just enough for the RUNPATH patcher) ----


def _build_minimal_elf(runpath: str) -> bytes:
    dynstr = b"\x00" + runpath.encode("ascii") + b"\x00"
    runpath_off = 1
    dynamic = struct.pack("<qQ", 29, runpath_off) + struct.pack("<qQ", 0, 0)
    shstrtab = b"\x00.dynstr\x00.dynamic\x00.shstrtab\x00"

    ehsize = 64
    off_dynstr = ehsize
    off_dynamic = off_dynstr + len(dynstr)
    off_shstrtab = off_dynamic + len(dynamic)
    shoff = off_shstrtab + len(shstrtab)
    shoff += (-shoff) % 8  # 8-byte align

    def shdr(name: int, sh_type: int, offset: int, size: int, entsize: int = 0) -> bytes:
        return struct.pack(
            "<IIQQQQIIQQ",
            name, sh_type, 0, 0, offset, size, 0, 0, 1, entsize,
        )

    shtable = (
        shdr(0, 0, 0, 0)
        + shdr(shstrtab.index(b".dynstr"), 3, off_dynstr, len(dynstr))
        + shdr(shstrtab.index(b".dynamic"), 6, off_dynamic, len(dynamic), 16)
        + shdr(shstrtab.index(b".shstrtab"), 3, off_shstrtab, len(shstrtab))
    )

    e_ident = b"\x7fELF" + bytes([2, 1, 1, 0]) + b"\x00" * 8
    header = e_ident + struct.pack(
        "<HHIQQQIHHHHHH",
        2, 0x3E, 1, 0, 0, shoff, 0, ehsize, 0, 0, 64, 4, 3,
    )
    assert len(header) == 64

    body = bytearray(header)
    body += dynstr + dynamic + shstrtab
    body += b"\x00" * (shoff - len(body))
    body += shtable
    return bytes(body)


def test_minimal_elf_builder_roundtrips() -> None:
    data = _build_minimal_elf("/mnt/q2rtx/.")
    found = _find_runpath_string(data)
    assert found is not None
    _offset, value = found
    assert value == "/mnt/q2rtx/."


def test_patch_runpath_rewrites_build_path_to_origin(tmp_path: Path) -> None:
    binary = tmp_path / "q2rtx"
    binary.write_bytes(_build_minimal_elf("/mnt/q2rtx/."))

    previous = patch_runpath_to_origin(binary)

    assert previous == "/mnt/q2rtx/."
    found = _find_runpath_string(binary.read_bytes())
    assert found is not None and found[1] == ORIGIN_RUNPATH
    # In-place edit: the file must not change size.
    assert binary.stat().st_size == len(_build_minimal_elf("/mnt/q2rtx/."))
    # The stale build path must be gone.
    assert b"/mnt/q2rtx" not in binary.read_bytes()


def test_patch_runpath_is_idempotent(tmp_path: Path) -> None:
    binary = tmp_path / "q2rtx"
    binary.write_bytes(_build_minimal_elf("/mnt/q2rtx/."))

    assert patch_runpath_to_origin(binary) == "/mnt/q2rtx/."
    assert patch_runpath_to_origin(binary) == ORIGIN_RUNPATH


def test_patch_runpath_refuses_when_slot_too_short(tmp_path: Path) -> None:
    binary = tmp_path / "q2rtx"
    original = _build_minimal_elf("/x")  # 2 chars, cannot fit "$ORIGIN"
    binary.write_bytes(original)

    assert patch_runpath_to_origin(binary) is None
    assert binary.read_bytes() == original


def test_patch_runpath_ignores_non_elf(tmp_path: Path) -> None:
    binary = tmp_path / "not-an-elf"
    binary.write_bytes(b"this is not an ELF file" * 8)

    assert patch_runpath_to_origin(binary) is None


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
