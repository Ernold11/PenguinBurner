from __future__ import annotations

import json

import pytest

from auto_uv.persistence import interrupted_probe_crash_cache as crash_cache
from auto_uv.persistence import unsafe_voltage_blacklist_file as blacklist_file


def test_validation_accepts_obvious_normal_candidate_hard_hang() -> None:
    validation = crash_cache.interrupted_marker_crash_cache_validation(
        {
            "state": "probing",
            "phase": "candidate",
            "candidate_voltage_mv": 875,
            "lock_clock_mhz": 1919,
            "details": {
                "start_voltage_mv": 987,
                "initial_probe_clock_mhz": 1933.29,
            },
        }
    )

    assert validation["accepted"] is True
    assert validation["reason"] == "validated normal candidate hard-hang marker"
    assert validation["voltage_drop_from_start_pct"] == pytest.approx(11.3475)
    assert validation["target_clock_pct_of_baseline"] == pytest.approx(99.2608)


def test_validation_rejects_normal_candidate_without_near_baseline_clock() -> None:
    validation = crash_cache.interrupted_marker_crash_cache_validation(
        {
            "state": "probing",
            "phase": "candidate",
            "candidate_voltage_mv": 875,
            "lock_clock_mhz": 1700,
            "details": {
                "start_voltage_mv": 987,
                "initial_probe_clock_mhz": 1933.29,
            },
        }
    )

    assert validation["accepted"] is False
    assert validation["target_clock_pct_of_baseline"] < 95.0


def test_validation_keeps_existing_aggressive_recovery_path() -> None:
    validation = crash_cache.interrupted_marker_crash_cache_validation(
        {
            "state": "probing",
            "phase": "final-verify",
            "candidate_voltage_mv": 875,
            "lock_clock_mhz": 1919,
            "details": {
                "start_voltage_mv": 987,
                "clock_recovery_pct": 125.0,
            },
        }
    )

    assert validation["accepted"] is True
    assert validation["reason"] == "validated aggressive undervolt crash marker"


def test_consume_records_obvious_normal_candidate_hard_hang(
    tmp_path,
    monkeypatch,
) -> None:
    marker_path = tmp_path / "auto-uv-probe-in-progress.json"
    unsafe_path = tmp_path / "auto-uv-unsafe-voltages.json"
    marker_path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "state": "probing",
                "phase": "candidate",
                "candidate_voltage_mv": 875,
                "lock_clock_mhz": 1919,
                "pid": 1234,
                "host": "test-host",
                "started_at": "2026-05-10T06:57:12+02:00",
                "log_context": "lower-voltage 875mV",
                "details": {
                    "start_voltage_mv": 987,
                    "initial_probe_clock_mhz": 1933.29,
                    "target_clock_pct_of_baseline": 99.2603,
                    "blocked_lock_clock_mhz": [1919, 1905],
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(crash_cache, "probe_in_progress_path", lambda: marker_path)
    monkeypatch.setattr(
        blacklist_file,
        "unsafe_voltage_blacklist_path",
        lambda: unsafe_path,
    )

    consumed = crash_cache.consume_interrupted_probe_crash_marker()

    assert consumed is not None
    path, entry = consumed
    assert path == unsafe_path
    assert marker_path.exists() is False
    assert entry["reason"] == "previous-run-abruptly-ended"
    assert entry["candidate_voltage_mv"] == 875
    assert entry["lock_clock_mhz"] == 1919
    assert entry["blocked_lock_clock_mhz"] == [1919, 1905]
    assert (
        entry["details"]["crash_cache_validation"]["reason"]
        == "validated normal candidate hard-hang marker"
    )
