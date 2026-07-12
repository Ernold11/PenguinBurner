from __future__ import annotations

import json

from auto_uv.curve.verified_envelope import (
    ENVELOPE_LOCK_HEADROOM_MHZ,
    apply_verified_envelope_below_lock,
    passed_probe_points,
    reshape_profile_file_below_lock,
)


def _point(voltage_mv: int, base_mhz: int, target_mhz: int | None = None) -> dict:
    target = base_mhz if target_mhz is None else target_mhz
    return {
        "index": voltage_mv,
        "voltage_mv": voltage_mv,
        "base_mhz": base_mhz,
        "target_mhz": target,
        "new_offset_mhz": target - base_mhz,
    }


PLAN = [
    _point(850, 2160),
    _point(875, 2272),
    _point(885, 2295, 2867),  # validated flatten region starts here
    _point(920, 2445, 2970),  # lock
    _point(935, 2490, 3000),  # rising tail
]

PASSED = [
    (850, 2737),
    (860, 2747),
    (875, 2885),
    (920, 2970),
    (935, 3000),
]


def test_envelope_raises_only_below_lock_and_stays_under_the_lock() -> None:
    shaped, raised = apply_verified_envelope_below_lock(
        PLAN, lock_voltage_mv=920, lock_clock_mhz=2970, passed=PASSED
    )

    by_voltage = {point["voltage_mv"]: point for point in shaped}
    assert raised == 3
    assert by_voltage[850]["target_mhz"] == 2737
    assert by_voltage[850]["new_offset_mhz"] == 2737 - 2160
    assert by_voltage[875]["target_mhz"] == 2885
    # The flatten bin below the lock may be raised too (a passed probe at
    # 875mV proved 2885), but never lowered.
    assert by_voltage[885]["target_mhz"] == 2885
    # Lock and rising tail untouched.
    assert by_voltage[920]["target_mhz"] == 2970
    assert by_voltage[935]["target_mhz"] == 3000
    # Monotone and capped below the lock clock.
    previous = 0
    for point in shaped:
        if point["voltage_mv"] < 920:
            assert point["target_mhz"] <= 2970 - ENVELOPE_LOCK_HEADROOM_MHZ
        assert point["target_mhz"] >= previous
        previous = point["target_mhz"]


def test_envelope_without_probes_or_lock_changes_nothing() -> None:
    shaped, raised = apply_verified_envelope_below_lock(
        PLAN, lock_voltage_mv=920, lock_clock_mhz=2970, passed=[]
    )
    assert raised == 0
    assert shaped == PLAN

    shaped, raised = apply_verified_envelope_below_lock(
        PLAN, lock_voltage_mv=920, lock_clock_mhz=0, passed=PASSED
    )
    assert raised == 0


def test_passed_probe_points_trusts_blacklist_failures_over_passes() -> None:
    candidates = [
        {"candidate_voltage_mv": 875, "lock_clock_mhz": 2885},
        {"candidate_voltage_mv": 860, "lock_clock_mhz": 2747},
        {"candidate_voltage_mv": 0, "lock_clock_mhz": 100},  # junk dropped
    ]
    unsafe = [{"candidate_voltage_mv": 875, "lock_clock_mhz": 2800}]

    points = passed_probe_points(candidates, unsafe_entries=unsafe)

    # The 875mV pass is contradicted by a recorded failure at a LOWER clock
    # at the same voltage: trust the failure, drop the pass.
    assert points == [(860, 2747)]


def test_reshape_profile_file_round_trips_with_backup(tmp_path, monkeypatch) -> None:
    import auto_uv.curve.verified_envelope as envelope

    monkeypatch.setattr(envelope, "passed_probe_points", lambda: PASSED)
    profile_path = tmp_path / "auto-uv-profile-test.json"
    profile_path.write_text(
        json.dumps(
            {
                "profile_id": "test",
                "candidate_voltage_mv": 920,
                "lock_clock_mhz": 2970,
                "points": PLAN,
                "plan": PLAN,
            }
        ),
        encoding="utf-8",
    )

    summary = envelope.reshape_profile_file_below_lock(profile_path)

    assert summary["raised"] == 3
    reshaped = json.loads(profile_path.read_text())
    assert reshaped["points"][1]["target_mhz"] == 2885
    assert reshaped["plan"][1]["target_mhz"] == 2885
    backup = tmp_path / "auto-uv-profile-test.json.pre-envelope.bak"
    assert backup.exists()
    assert json.loads(backup.read_text())["points"][1]["target_mhz"] == 2272

    # Idempotent: a second reshape raises nothing further.
    summary = envelope.reshape_profile_file_below_lock(profile_path)
    assert summary["raised"] == 0
