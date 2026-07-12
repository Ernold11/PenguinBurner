"""Fill below-lock V/F bins from the archive of probe-passed candidates.

The scan proves one lock point per tier, but on its way there the descent
probes (and passes) many (voltage, clock) points; they accumulate in
``auto-uv-verified-candidates.json``. Shipping stock values below the lock
creates a discontinuity — e.g. 875 mV → 2272 MHz stock next to a validated
885 mV → 2867 MHz bin — even though the archive holds a *passed* probe at
875 mV → 2885 MHz for the same GPU.

This module raises each below-lock bin to the best clock the archive proved
at that bin's voltage *or lower* (running a point proven at lower voltage at
a higher one only adds margin). The envelope is monotone by construction
(prefix maximum over voltage), never lowers a bin below the scan plan's own
value, stays a full clock step below the lock clock so the lock remains the
distinguished operating point, and skips any probe contradicted by the
unsafe-voltage blacklist. No point is fabricated: every raised bin is backed
by a probe this GPU actually passed.

Unlike the retired ``tier_runtime_profile`` shaping, nothing here is
extrapolated geometry — bins with no passed probe at-or-below their voltage
keep their stock values.
"""

from __future__ import annotations

from pathlib import Path

from auto_uv.persistence.auto_uv_persisted_json_files import safe_json_write
from auto_uv.persistence.unsafe_voltage_blacklist_file import (
    load_unsafe_voltage_blacklist,
)
from auto_uv.persistence.verified_candidate_result_file import (
    read_verified_candidates,
)

# The envelope tops out this far below the lock clock: the lock point the
# 5-minute final verification proved stays the distinguished maximum of the
# below-lock region (one NVIDIA V/F clock step).
ENVELOPE_LOCK_HEADROOM_MHZ = 15


def passed_probe_points(
    candidates: list[dict] | None = None,
    *,
    unsafe_entries: list[dict] | None = None,
) -> list[tuple[int, int]]:
    """(voltage_mv, lock_clock_mhz) pairs this GPU passed, blacklist-filtered.

    Every entry in the verified-candidates archive represents a PASSED probe
    (short descent probes and final-verified locks alike). A pass that the
    unsafe blacklist contradicts — a recorded failure at the same voltage
    with an equal-or-lower clock — is dropped: trust the failure.
    """
    if candidates is None:
        candidates = read_verified_candidates()
    if unsafe_entries is None:
        unsafe_entries = load_unsafe_voltage_blacklist()
    unsafe: list[tuple[int, int]] = []
    for entry in unsafe_entries:
        try:
            unsafe.append(
                (int(entry["candidate_voltage_mv"]), int(entry["lock_clock_mhz"]))
            )
        except (KeyError, TypeError, ValueError):
            continue

    points: list[tuple[int, int]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        try:
            voltage_mv = int(candidate["candidate_voltage_mv"])
            clock_mhz = int(candidate["lock_clock_mhz"])
        except (KeyError, TypeError, ValueError):
            continue
        if voltage_mv <= 0 or clock_mhz <= 0:
            continue
        if any(
            unsafe_voltage == voltage_mv and unsafe_clock <= clock_mhz
            for unsafe_voltage, unsafe_clock in unsafe
        ):
            continue
        points.append((voltage_mv, clock_mhz))
    return points


def apply_verified_envelope_below_lock(
    plan: list[dict],
    *,
    lock_voltage_mv: int,
    lock_clock_mhz: int,
    passed: list[tuple[int, int]] | None = None,
) -> tuple[list[dict], int]:
    """Raise below-lock plan bins to the passed-probe envelope.

    Returns the new plan and the number of bins raised. Bins at or above the
    lock voltage (the verified flat region and rising tail) are untouched.
    """
    if passed is None:
        passed = passed_probe_points()
    lock_voltage_mv = int(lock_voltage_mv)
    cap_mhz = int(lock_clock_mhz) - ENVELOPE_LOCK_HEADROOM_MHZ
    if cap_mhz <= 0 or not passed:
        return [dict(point) for point in plan], 0

    # Prefix maximum over voltage: env(v) = best clock proven at <= v.
    proven = sorted(passed)
    envelope: list[tuple[int, int]] = []
    best = 0
    for voltage_mv, clock_mhz in proven:
        best = max(best, clock_mhz)
        envelope.append((voltage_mv, best))

    def env_at(voltage_mv: int) -> int:
        value = 0
        for env_voltage, env_clock in envelope:
            if env_voltage > voltage_mv:
                break
            value = env_clock
        return value

    raised = 0
    shaped: list[dict] = []
    for point in plan:
        new_point = dict(point)
        try:
            voltage_mv = int(point["voltage_mv"])
            base_mhz = int(point["base_mhz"])
            target_mhz = int(point["target_mhz"])
        except (KeyError, TypeError, ValueError):
            shaped.append(new_point)
            continue
        if voltage_mv < lock_voltage_mv:
            proven_mhz = min(env_at(voltage_mv), cap_mhz)
            if proven_mhz > target_mhz:
                new_point["target_mhz"] = proven_mhz
                new_point["new_offset_mhz"] = proven_mhz - base_mhz
                raised += 1
        shaped.append(new_point)
    return shaped, raised


def reshape_profile_file_below_lock(path: str | Path) -> dict:
    """Re-shape an existing saved profile's below-lock bins from the archive.

    Writes the profile back in place (atomic) with a ``.pre-envelope.bak``
    sibling of the original. Returns a summary dict.
    """
    import json

    path = Path(path)
    payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    lock_voltage_mv = int(payload.get("candidate_voltage_mv") or 0)
    lock_clock_mhz = int(payload.get("lock_clock_mhz") or 0)
    if lock_voltage_mv <= 0 or lock_clock_mhz <= 0:
        return {"path": str(path), "raised": 0, "reason": "no lock point"}
    passed = passed_probe_points()
    total_raised = 0
    for key in ("points", "plan"):
        plan = payload.get(key)
        if not isinstance(plan, list) or not plan:
            continue
        shaped, raised = apply_verified_envelope_below_lock(
            plan,
            lock_voltage_mv=lock_voltage_mv,
            lock_clock_mhz=lock_clock_mhz,
            passed=passed,
        )
        payload[key] = shaped
        total_raised = max(total_raised, raised)
    if total_raised:
        backup = path.with_name(path.name + ".pre-envelope.bak")
        if not backup.exists():
            backup.write_text(
                path.read_text(encoding="utf-8", errors="replace"),
                encoding="utf-8",
            )
        safe_json_write(path, payload)
    return {"path": str(path), "raised": total_raised, "reason": "ok"}
