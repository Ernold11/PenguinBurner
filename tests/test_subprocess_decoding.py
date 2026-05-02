from __future__ import annotations

from pathlib import Path
import json

from auto_uv3.persistence import unsafe_voltage_blacklist_file
import saved_uv_profiles.profile_store as profile_store
import penguin_burner
import pytest
from saved_uv_profiles import archive_auto_uv_profile


def test_nvidia_smi_output_with_invalid_utf8_is_tolerated(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_nvidia_smi = tmp_path / "nvidia-smi"
    fake_nvidia_smi.write_bytes(
        b"#!/bin/sh\nprintf 'gpu ok \\233 bad byte\\n'\n"
    )
    fake_nvidia_smi.chmod(0o755)

    monkeypatch.setattr(penguin_burner, "NVIDIA_SMI", str(fake_nvidia_smi))

    assert "gpu ok" in penguin_burner.run_nvidia_smi(["--query-gpu=name"])


def test_invalid_utf8_in_unsafe_voltage_state_does_not_crash(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state_path = tmp_path / "auto-uv-unsafe-voltages.json"
    state_path.write_bytes(
        b'{"entries": [{"reason": "bad \x9b byte", '
        b'"candidate_voltage_mv": 900, "lock_clock_mhz": 2500}]}'
    )

    monkeypatch.setattr(
        unsafe_voltage_blacklist_file,
        "unsafe_voltage_blacklist_path",
        lambda: state_path,
    )

    entries = unsafe_voltage_blacklist_file.load_unsafe_voltage_blacklist()

    assert entries[0]["reason"].startswith("bad ")


def test_invalid_utf8_in_profile_path_does_not_report_codec_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    profile_path = tmp_path / "bad-profile.json"
    profile_path.write_bytes(b"\x9b")

    monkeypatch.setattr(penguin_burner, "default_user_config_dir", lambda: tmp_path)

    try:
        penguin_burner.load_auto_uv_final_curve(str(profile_path))
    except Exception as exc:
        assert "codec" not in str(exc).lower()
    else:
        raise AssertionError("expected invalid JSON to fail")


def test_load_auto_uv_final_curve_rejects_user_edited_draft_by_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)
    profile_path = archive_auto_uv_profile(
        {
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2600,
            "profile_source": "user-edited",
            "final_verified": False,
            "requires_verification": True,
            "points": [
                {
                    "index": 0,
                    "voltage_mv": 900,
                    "base_mhz": 2500,
                    "target_mhz": 2600,
                    "new_offset_mhz": 100,
                }
            ],
        }
    )

    with pytest.raises(penguin_burner.NvmlError):
        penguin_burner.load_auto_uv_final_curve(str(profile_path))

    loaded = penguin_burner.load_auto_uv_final_curve(
        str(profile_path),
        allow_unverified=True,
    )
    assert loaded is not None
    assert loaded["candidate_voltage_mv"] == 900


def test_json_events_omit_none_values(capsys) -> None:
    penguin_burner.emit_json_event(
        True,
        "load_telemetry",
        target_duration_s=None,
        elapsed_s=1.25,
        nested={"known": 1, "unknown": None},
        values=[1, None, {"kept": True, "dropped": None}],
    )

    payload = json.loads(capsys.readouterr().out)

    assert payload == {
        "event": "load_telemetry",
        "elapsed_s": 1.25,
        "nested": {"known": 1},
        "values": [1, {"kept": True}],
    }
