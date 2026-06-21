from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Callable

from overlay.config import DEFAULT_OVERLAY_UPDATE_INTERVAL_S
from overlay.config import MAX_OVERLAY_UPDATE_INTERVAL_S
from overlay.config import MIN_OVERLAY_UPDATE_INTERVAL_S
from overlay.config import load_overlay_config
from overlay.state import OverlayState, write_overlay_state
from profiles.uv.profile_tiers import profile_tier_label

from .live_gpu_telemetry_text import get_core_clock_mhz
from .live_gpu_telemetry_text import get_gpu_utilization_pct
from .live_gpu_telemetry_text import get_power_draw_w
from .live_gpu_telemetry_text import get_reported_fan_speeds


@dataclass(slots=True)
class OverlayStatePublisher:
    gpu_index: int
    nvml_session: object
    voltage_reader: object | None
    profile_tier: str
    vf_curve_reader: object | None = None
    enabled: bool = True
    update_interval_s: float = DEFAULT_OVERLAY_UPDATE_INTERVAL_S
    profile_tier_key: str = ""
    profile_id: str = ""
    adaptive: bool = False
    process_cpu_sampler: object | None = None
    path: str | Path | None = None
    config_path: str | Path | None = None
    overlay_config_loader: Callable = load_overlay_config
    time_ns: Callable[[], int] = time.time_ns
    _last_cpu_util_pct: int | None = field(default=None, init=False, repr=False)
    _last_cpu_peak_thread_pct: int | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _last_cpu_metric_ns: int | None = field(default=None, init=False, repr=False)
    _last_published_gpu_util_pct: int | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _last_published_cpu_util_pct: int | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _last_published_cpu_peak_thread_pct: int | None = field(
        default=None,
        init=False,
        repr=False,
    )

    @property
    def last_gpu_util_pct(self) -> int | None:
        return self._last_published_gpu_util_pct

    @property
    def last_cpu_util_pct(self) -> int | None:
        return self._last_published_cpu_util_pct

    @property
    def last_cpu_peak_thread_pct(self) -> int | None:
        return self._last_published_cpu_peak_thread_pct

    def publish(self, *, latency_snapshot: dict | None = None) -> Path:
        self.refresh_config()
        now_ns = int(self.time_ns())
        clock_mhz = None
        try:
            clock_mhz = get_core_clock_mhz(
                self.nvml_session.nvml,
                self.nvml_session.device,
            )
        except Exception:
            clock_mhz = None

        power_w = None
        try:
            power_draw_w = get_power_draw_w(
                self.nvml_session.nvml,
                self.nvml_session.device,
            )
        except Exception:
            power_draw_w = None
        if power_draw_w is not None:
            try:
                power_w = int(round(float(power_draw_w)))
            except (TypeError, ValueError):
                power_w = None

        gpu_util_pct = None
        try:
            gpu_util_pct = get_gpu_utilization_pct(
                self.nvml_session.nvml,
                self.nvml_session.device,
            )
        except Exception:
            gpu_util_pct = None
        self._last_published_gpu_util_pct = _int_or_none(gpu_util_pct)

        cpu_util_pct = None
        cpu_peak_thread_pct = None
        if self.process_cpu_sampler is not None:
            try:
                cpu_pid = _pid_from_latency_snapshot(latency_snapshot)
                if cpu_pid not in (None, ""):
                    sample_usage = getattr(
                        self.process_cpu_sampler,
                        "sample_usage",
                        None,
                    )
                    if callable(sample_usage):
                        usage = sample_usage(cpu_pid)
                        cpu_util_pct = getattr(usage, "process_util_pct", None)
                        cpu_peak_thread_pct = getattr(
                            usage,
                            "peak_thread_util_pct",
                            None,
                        )
                    else:
                        cpu_util_pct = self.process_cpu_sampler.sample(cpu_pid)
            except Exception:
                cpu_util_pct = None
                cpu_peak_thread_pct = None
        self._remember_cpu_metrics(
            now_ns=now_ns,
            cpu_util_pct=cpu_util_pct,
            cpu_peak_thread_pct=cpu_peak_thread_pct,
        )
        cpu_util_pct, cpu_peak_thread_pct = self._sticky_cpu_metrics(now_ns)
        self._last_published_cpu_util_pct = cpu_util_pct
        self._last_published_cpu_peak_thread_pct = cpu_peak_thread_pct

        fan_pct = None
        try:
            fan_count = int(self.nvml_session.fan_count())
            speeds = get_reported_fan_speeds(
                self.nvml_session.nvml,
                self.nvml_session.device,
                fan_count,
            )
        except Exception:
            speeds = None
        if speeds:
            try:
                fan_pct = int(round(sum(float(speed) for speed in speeds) / len(speeds)))
            except (TypeError, ValueError, ZeroDivisionError):
                fan_pct = None

        temperature_c = None
        try:
            temperature_c = int(round(float(self.nvml_session.temperature_c())))
        except Exception:
            temperature_c = None

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

        uv_offset_mv = _uv_offset_mv(
            self.vf_curve_reader,
            core_clock_mhz=clock_mhz,
            voltage_mv=voltage_mv,
        )
        label = str(self.profile_tier or "").strip()
        if not label:
            label = profile_tier_label(self.profile_tier_key) or "Balanced"
        present_fps = ""
        framegen_fps = ""
        framegen_active = False
        latency_ms = ""
        display_latency_ms = ""
        if isinstance(latency_snapshot, dict):
            present_fps = str(latency_snapshot.get("present_fps") or "").strip()
            framegen_fps = _framegen_fps_from_snapshot(latency_snapshot)
            framegen_active = _flag_enabled(latency_snapshot.get("framegen_active"))
            latency_p95_ms = latency_snapshot.get("latency_p95_ms")
            if latency_p95_ms is not None:
                try:
                    latency_ms = str(int(round(float(latency_p95_ms))))
                except (TypeError, ValueError):
                    latency_ms = ""
            # Present->scanout tail kept separate from latency_ms; the overlay
            # sums the two into the displayed click-to-photon number.
            display_latency_p95_ms = latency_snapshot.get("display_latency_p95_ms")
            if display_latency_p95_ms is not None:
                try:
                    display_latency_ms = str(int(round(float(display_latency_p95_ms))))
                except (TypeError, ValueError):
                    display_latency_ms = ""
        return write_overlay_state(
            OverlayState(
                gpu_index=int(self.gpu_index),
                clock_mhz=clock_mhz,
                voltage_mv=voltage_mv,
                power_w=power_w,
                gpu_util_pct=gpu_util_pct,
                cpu_util_pct=cpu_util_pct,
                cpu_peak_thread_pct=cpu_peak_thread_pct,
                fan_pct=fan_pct,
                temperature_c=temperature_c,
                uv_offset_mv=uv_offset_mv,
                profile_tier=label,
                present_fps=present_fps,
                framegen_fps=framegen_fps,
                framegen_active=framegen_active,
                latency_ms=latency_ms,
                display_latency_ms=display_latency_ms,
                profile_tier_key=str(self.profile_tier_key or ""),
                profile_id=str(self.profile_id or ""),
                adaptive=bool(self.adaptive),
                updated_unix_ns=now_ns,
            ),
            path=self.path,
        )

    def refresh_config(self) -> None:
        if self.config_path is None:
            return
        try:
            config = self.overlay_config_loader(self.config_path)
        except Exception:
            return
        self.enabled = bool(getattr(config, "enabled", self.enabled))
        self.update_interval_s = getattr(
            config,
            "update_interval_s",
            self.update_interval_s,
        )

    def _remember_cpu_metrics(
        self,
        *,
        now_ns: int,
        cpu_util_pct: object,
        cpu_peak_thread_pct: object,
    ) -> None:
        cpu_value = _int_or_none(cpu_util_pct)
        peak_value = _int_or_none(cpu_peak_thread_pct)
        if cpu_value is None and peak_value is None:
            return
        if cpu_value is not None:
            self._last_cpu_util_pct = cpu_value
        if peak_value is not None:
            self._last_cpu_peak_thread_pct = peak_value
        self._last_cpu_metric_ns = int(now_ns)

    def _sticky_cpu_metrics(self, now_ns: int) -> tuple[int | None, int | None]:
        if self._last_cpu_metric_ns is None:
            return None, None
        if int(now_ns) - int(self._last_cpu_metric_ns) > self._cpu_metric_hold_ns():
            return None, None
        return self._last_cpu_util_pct, self._last_cpu_peak_thread_pct

    def _cpu_metric_hold_ns(self) -> int:
        try:
            interval_s = float(self.update_interval_s)
        except (TypeError, ValueError):
            interval_s = float(DEFAULT_OVERLAY_UPDATE_INTERVAL_S)
        interval_s = max(
            float(MIN_OVERLAY_UPDATE_INTERVAL_S),
            min(float(MAX_OVERLAY_UPDATE_INTERVAL_S), interval_s),
        )
        return int(round(interval_s * 1_000_000_000))


def _framegen_fps_from_snapshot(snapshot: dict) -> str:
    stats = snapshot.get("raw_present_fps_stats")
    if isinstance(stats, dict):
        text = str(stats.get("avg") or "").strip()
        if text and text.lower() != "n/a":
            return text

    value = snapshot.get("raw_present_fps_avg")
    if value is None:
        return ""
    try:
        return str(int(round(float(value))))
    except (TypeError, ValueError):
        return ""


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _pid_from_latency_snapshot(snapshot: dict | None) -> object | None:
    if not isinstance(snapshot, dict):
        return None
    pid = snapshot.get("pid")
    if pid not in (None, ""):
        return pid
    samples = snapshot.get("samples")
    if isinstance(samples, list):
        for sample in reversed(samples):
            if isinstance(sample, dict) and sample.get("pid") not in (None, ""):
                return sample.get("pid")
    return None


def _flag_enabled(value: object) -> bool:
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "on", "active"}


def _uv_offset_mv(vf_curve_reader, *, core_clock_mhz, voltage_mv) -> int | None:
    if vf_curve_reader is None or core_clock_mhz is None or voltage_mv is None:
        return None
    try:
        vf_curve_reader.refresh_points()
    except Exception:
        pass
    try:
        point = vf_curve_reader.find_nearest_point(
            int(core_clock_mhz),
            int(voltage_mv) * 1000,
        )
        base_points = vf_curve_reader.editable_core_points()
    except Exception:
        return None
    if point is None or not base_points:
        return None
    try:
        base_point = min(
            base_points,
            key=lambda candidate: abs(
                int(candidate["base_freq_khz"]) - int(core_clock_mhz) * 1000
            ),
        )
        point_voltage_mv = int(point["voltage_uv"] // 1000)
        base_voltage_mv = int(base_point["voltage_uv"] // 1000)
    except Exception:
        return None
    return int(point_voltage_mv - base_voltage_mv)
