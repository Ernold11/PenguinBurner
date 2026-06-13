from __future__ import annotations

import csv
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
import shutil
import subprocess

from afterburner.import_fan_curve import write_config
from cli.runtime_config_file import load_raw_runtime_config
from common.penguin_burner_paths import default_runtime_config_path


@dataclass(frozen=True)
class GpuChoice:
    index: int
    name: str
    pci_bus_id: str = ""
    uuid: str = ""

    @property
    def label(self) -> str:
        name = self.name.strip() or "NVIDIA GPU"
        bus = _short_bus_id(self.pci_bus_id)
        if bus:
            return f"GPU {self.index} - {name} ({bus})"
        return f"GPU {self.index} - {name}"


def detected_gpu_choices() -> list[GpuChoice]:
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return []
    try:
        result = subprocess.run(
            [
                nvidia_smi,
                "--query-gpu=index,name,pci.bus_id,uuid",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return parse_nvidia_smi_gpu_choices(result.stdout)


def parse_nvidia_smi_gpu_choices(output: str) -> list[GpuChoice]:
    choices: list[GpuChoice] = []
    seen: set[int] = set()
    for fields in csv.reader(StringIO(str(output or "")), skipinitialspace=True):
        fields = [part.strip() for part in fields]
        if len(fields) < 2:
            continue
        try:
            index = max(0, int(fields[0]))
        except ValueError:
            continue
        if index in seen:
            continue
        seen.add(index)
        choices.append(
            GpuChoice(
                index=index,
                name=fields[1],
                pci_bus_id=fields[2] if len(fields) >= 3 else "",
                uuid=fields[3] if len(fields) >= 4 else "",
            )
        )
    return choices


def runtime_gpu_index(config_path: str | Path | None = None) -> int:
    path = default_runtime_config_path() if config_path is None else Path(config_path)
    try:
        config = load_raw_runtime_config(path)
        return max(0, int(config.get("gpu", {}).get("index", 0)))
    except Exception:
        return 0


def gpu_choices_with_fallback(
    *,
    selected_index: int | None = None,
    config_path: str | Path | None = None,
    choices: list[GpuChoice] | None = None,
) -> tuple[list[GpuChoice], int]:
    selected = (
        runtime_gpu_index(config_path)
        if selected_index is None
        else max(0, int(selected_index))
    )
    detected = list(detected_gpu_choices() if choices is None else choices)
    if not detected:
        return [GpuChoice(index=selected, name="NVIDIA GPU")], selected
    if selected not in {choice.index for choice in detected}:
        detected.append(GpuChoice(index=selected, name="NVIDIA GPU"))
        detected.sort(key=lambda choice: choice.index)
    return detected, selected


def persist_runtime_gpu_index(
    gpu_index: int,
    *,
    config_path: str | Path | None = None,
) -> int:
    selected = max(0, int(gpu_index))
    path = default_runtime_config_path() if config_path is None else Path(config_path)
    try:
        config = load_raw_runtime_config(path)
    except Exception:
        config = {}
    updated = dict(config)
    gpu = dict(updated.get("gpu", {}))
    gpu["index"] = selected
    updated["gpu"] = gpu
    write_config(path, updated)
    return selected


def _short_bus_id(bus_id: str) -> str:
    text = str(bus_id or "").strip()
    if not text:
        return ""
    if ":" in text:
        return text.split(":", 1)[1]
    return text
