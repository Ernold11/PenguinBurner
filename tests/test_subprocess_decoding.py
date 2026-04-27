from __future__ import annotations

from pathlib import Path
import json

import auto_uv.artifacts as auto_uv_artifacts
import penguin_burner


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
    state_path.write_bytes(b'{"entries": [{"reason": "bad \x9b byte"}]}')

    monkeypatch.setattr(
        auto_uv_artifacts,
        "_unsafe_voltage_blacklist_path",
        lambda: state_path,
    )

    entries = auto_uv_artifacts._load_uv_unsafe_voltage_entries()

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
