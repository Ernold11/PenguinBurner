from __future__ import annotations

from pathlib import Path

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
