from __future__ import annotations

from pathlib import Path

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


def test_invalid_utf8_in_final_curve_state_reports_json_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_dir = tmp_path
    (config_dir / "auto-uv-final-curve.json").write_bytes(b"\x9b")

    monkeypatch.setattr(penguin_burner, "default_user_config_dir", lambda: config_dir)

    try:
        penguin_burner.load_auto_uv_final_curve()
    except Exception as exc:
        assert "codec" not in str(exc).lower()
    else:
        raise AssertionError("expected invalid JSON to fail")
