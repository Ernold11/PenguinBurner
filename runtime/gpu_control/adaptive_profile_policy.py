"""Adaptive policy values authored by Python for the Rust runtime spec.

The adaptive state machine itself lives only in ``penguin-burnerd``.  Python
keeps this small configuration object because it resolves user/environment
intent before sending an immutable RuntimeSpec to the daemon.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
import os

from runtime.support.adaptive_target_fps import adaptive_target_ms_from_fps


ADAPTIVE_TARGET_SLOW_WINDOWS_ENV = "PENGUIN_BURNER_ADAPTIVE_TARGET_SLOW_WINDOWS"
ADAPTIVE_NEAR_SLOW_WINDOWS_ENV = "PENGUIN_BURNER_ADAPTIVE_NEAR_SLOW_WINDOWS"
ADAPTIVE_COMFORT_WINDOWS_ENV = "PENGUIN_BURNER_ADAPTIVE_COMFORT_WINDOWS"
ADAPTIVE_PERFORMANCE_COMFORT_WINDOWS_ENV = (
    "PENGUIN_BURNER_ADAPTIVE_PERFORMANCE_COMFORT_WINDOWS"
)
ADAPTIVE_DEMOTE_DWELL_S_ENV = "PENGUIN_BURNER_ADAPTIVE_DEMOTE_DWELL_S"
ADAPTIVE_PERFORMANCE_DEMOTE_DWELL_S_ENV = (
    "PENGUIN_BURNER_ADAPTIVE_PERFORMANCE_DEMOTE_DWELL_S"
)
ADAPTIVE_CPU_BOUND_GPU_UTIL_MAX_ENV = (
    "PENGUIN_BURNER_ADAPTIVE_CPU_BOUND_GPU_UTIL_MAX"
)
ADAPTIVE_CPU_BOUND_PEAK_THREAD_MIN_ENV = (
    "PENGUIN_BURNER_ADAPTIVE_CPU_BOUND_PEAK_THREAD_MIN"
)
ADAPTIVE_CPU_BOUND_PROCESS_UTIL_MIN_ENV = (
    "PENGUIN_BURNER_ADAPTIVE_CPU_BOUND_PROCESS_UTIL_MIN"
)


@dataclass(frozen=True, slots=True)
class AdaptiveProfilePolicyConfig:
    comfort_ms: float = 14.5
    target_ms: float = 16.6
    near_slow_ms: float = 18.5
    badly_slow_ms: float = 22.0
    target_slow_windows: int = 3
    near_slow_windows: int = 2
    comfort_windows: int = 6
    performance_comfort_windows: int = 10
    demote_dwell_s: float = 60.0
    performance_demote_dwell_s: float = 45.0
    # "CPU-bound, promoting the GPU tier cannot help" must mean exactly that:
    # the GPU clearly underused AND the CPU genuinely saturated (one pegged
    # thread, or very high overall process load). The old 85/70/12 defaults
    # held promotion for nearly every real game — a render thread idling at
    # 70% and 12% process CPU are both normal for GPU-bound gameplay.
    cpu_bound_gpu_util_max_pct: float = 60.0
    cpu_bound_peak_thread_min_pct: float = 97.0
    cpu_bound_process_util_min_pct: float = 60.0

    @classmethod
    def for_target_fps(
        cls,
        fps: object,
        *,
        env: Mapping[str, str] | None = None,
    ) -> "AdaptiveProfilePolicyConfig":
        default = cls()
        target_ms = adaptive_target_ms_from_fps(fps)
        scale = target_ms / default.target_ms
        config = cls(
            comfort_ms=default.comfort_ms * scale,
            target_ms=target_ms,
            near_slow_ms=default.near_slow_ms * scale,
            badly_slow_ms=default.badly_slow_ms * scale,
            target_slow_windows=default.target_slow_windows,
            near_slow_windows=default.near_slow_windows,
            comfort_windows=default.comfort_windows,
            performance_comfort_windows=default.performance_comfort_windows,
            demote_dwell_s=default.demote_dwell_s,
            performance_demote_dwell_s=default.performance_demote_dwell_s,
            cpu_bound_gpu_util_max_pct=default.cpu_bound_gpu_util_max_pct,
            cpu_bound_peak_thread_min_pct=default.cpu_bound_peak_thread_min_pct,
            cpu_bound_process_util_min_pct=default.cpu_bound_process_util_min_pct,
        )
        return config.with_env_overrides(env)

    def with_env_overrides(
        self,
        env: Mapping[str, str] | None = None,
    ) -> "AdaptiveProfilePolicyConfig":
        values: Mapping[str, str] = os.environ if env is None else env
        return replace(
            self,
            target_slow_windows=_env_int(
                values,
                ADAPTIVE_TARGET_SLOW_WINDOWS_ENV,
                self.target_slow_windows,
            ),
            near_slow_windows=_env_int(
                values,
                ADAPTIVE_NEAR_SLOW_WINDOWS_ENV,
                self.near_slow_windows,
            ),
            comfort_windows=_env_int(
                values,
                ADAPTIVE_COMFORT_WINDOWS_ENV,
                self.comfort_windows,
            ),
            performance_comfort_windows=_env_int(
                values,
                ADAPTIVE_PERFORMANCE_COMFORT_WINDOWS_ENV,
                self.performance_comfort_windows,
            ),
            demote_dwell_s=_env_float(
                values,
                ADAPTIVE_DEMOTE_DWELL_S_ENV,
                self.demote_dwell_s,
            ),
            performance_demote_dwell_s=_env_float(
                values,
                ADAPTIVE_PERFORMANCE_DEMOTE_DWELL_S_ENV,
                self.performance_demote_dwell_s,
            ),
            cpu_bound_gpu_util_max_pct=_env_percentage(
                values,
                ADAPTIVE_CPU_BOUND_GPU_UTIL_MAX_ENV,
                self.cpu_bound_gpu_util_max_pct,
            ),
            cpu_bound_peak_thread_min_pct=_env_percentage(
                values,
                ADAPTIVE_CPU_BOUND_PEAK_THREAD_MIN_ENV,
                self.cpu_bound_peak_thread_min_pct,
            ),
            cpu_bound_process_util_min_pct=_env_percentage(
                values,
                ADAPTIVE_CPU_BOUND_PROCESS_UTIL_MIN_ENV,
                self.cpu_bound_process_util_min_pct,
            ),
        )


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = str(env.get(name) or "").strip()
    if not raw:
        return int(default)
    try:
        value = int(raw)
    except ValueError:
        return int(default)
    return value if 1 <= value <= 120 else int(default)


def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = str(env.get(name) or "").strip()
    if not raw:
        return float(default)
    try:
        value = float(raw)
    except ValueError:
        return float(default)
    return value if 0.0 <= value <= 3600.0 else float(default)


def _env_percentage(env: Mapping[str, str], name: str, default: float) -> float:
    raw = str(env.get(name) or "").strip()
    if not raw:
        return float(default)
    try:
        value = float(raw)
    except ValueError:
        return float(default)
    return value if 0.0 <= value <= 100.0 else float(default)
