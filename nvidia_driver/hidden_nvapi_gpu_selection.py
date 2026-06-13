from __future__ import annotations

import re
import shutil
import subprocess


def query_nvidia_smi_pci_bus_id(gpu_index: int) -> str:
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return ""
    try:
        result = subprocess.run(
            [
                nvidia_smi,
                "--query-gpu=pci.bus_id",
                "--format=csv,noheader,nounits",
                "-i",
                str(int(gpu_index)),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if int(result.returncode) != 0:
        return ""
    lines = str(result.stdout or "").strip().splitlines()
    if not lines:
        return ""
    return lines[0].strip()


def pci_bus_number_from_bus_id(pci_bus_id: str) -> int | None:
    text = str(pci_bus_id or "").strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) >= 2:
        bus_text = parts[-2]
    else:
        match = re.search(r"(^|[^0-9a-fA-F])([0-9a-fA-F]{2})(?=[:._-])", text)
        bus_text = match.group(2) if match else ""
    try:
        return int(bus_text, 16)
    except ValueError:
        return None
