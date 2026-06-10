from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Callable

from penguin_burner_overlay.state import OverlayState, write_overlay_state
from saved_uv_profiles.profile_tiers import profile_tier_label

from .live_gpu_telemetry_text import get_core_clock_mhz


@dataclass(slots=True)
class OverlayStatePublisher:
    gpu_index: int
    nvml_session: object
    voltage_reader: object | None
    profile_tier: str
    profile_tier_key: str = ""
    profile_id: str = ""
    adaptive: bool = False
    path: str | Path | None = None
    time_ns: Callable[[], int] = time.time_ns

    def publish(self) -> Path:
        clock_mhz = None
        try:
            clock_mhz = get_core_clock_mhz(
                self.nvml_session.nvml,
                self.nvml_session.device,
            )
        except Exception:
            clock_mhz = None

        voltage_mv = None
        if self.voltage_reader is not None:
            try:
                voltage_uv = self.voltage_reader.read_microvolts(
                    self.nvml_session.device
                )
            except Exception:
                voltage_uv = None
            if voltage_uv is not None:
                voltage_mv = int(round(float(voltage_uv) / 1000.0))

        label = str(self.profile_tier or "").strip()
        if not label:
            label = profile_tier_label(self.profile_tier_key) or "Balanced"
        return write_overlay_state(
            OverlayState(
                gpu_index=int(self.gpu_index),
                clock_mhz=clock_mhz,
                voltage_mv=voltage_mv,
                profile_tier=label,
                profile_tier_key=str(self.profile_tier_key or ""),
                profile_id=str(self.profile_id or ""),
                adaptive=bool(self.adaptive),
                updated_unix_ns=int(self.time_ns()),
            ),
            path=self.path,
        )
