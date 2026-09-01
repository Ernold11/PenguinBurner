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
ADAPTIVE_FRAME_CAP_ENTER_GPU_PCT_ENV = "PENGUIN_BURNER_ADAPTIVE_FRAME_CAP_ENTER_GPU_PCT"
ADAPTIVE_FRAME_CAP_EXIT_GPU_PCT_ENV = "PENGUIN_BURNER_ADAPTIVE_FRAME_CAP_EXIT_GPU_PCT"
ADAPTIVE_FRAME_CAP_CONFIRM_WINDOWS_ENV = (
    "PENGUIN_BURNER_ADAPTIVE_FRAME_CAP_CONFIRM_WINDOWS"
)
ADAPTIVE_FRAME_CAP_EXIT_PACING_PCT_ENV = (
    "PENGUIN_BURNER_ADAPTIVE_FRAME_CAP_EXIT_PACING_PCT"
)
ADAPTIVE_DESKTOP_IDLE_GPU_PCT_ENV = "PENGUIN_BURNER_ADAPTIVE_DESKTOP_IDLE_GPU_PCT"
ADAPTIVE_DESKTOP_IDLE_AFTER_S_ENV = "PENGUIN_BURNER_ADAPTIVE_DESKTOP_IDLE_AFTER_S"
ADAPTIVE_RESPONSIVENESS_ENV = "PENGUIN_BURNER_ADAPTIVE_RESPONSIVENESS"

# The one-word preset: how quickly the whole state machine reacts. ``eager``
# halves every windows and dwell knob (reacts in half the time), ``relaxed``
# doubles them (twice the patience). Anything else means ``normal``.
_RESPONSIVENESS_FACTORS = {"eager": 0.5, "normal": 1.0, "relaxed": 2.0}


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
    # At or below this the GPU is plainly not what holds the frame rate back,
    # so a missed target means an external cap (a menu's frame lock, vsync),
    # the CPU, or IO -- none of which more clock can fix. Matches
    # cpu_bound_gpu_util_max_pct and the live sessions the feature was
    # validated under (capped games measured 51-58% at higher tiers); the
    # extra evidence a step DOWN demands comes from the confirm streak.
    frame_cap_enter_gpu_pct: float = 60.0
    # The other side of the same latch: at or above this the card is flat out,
    # so a recognised cap is dropped however steady pacing looks. Far above the
    # entry bar on purpose -- one shared number let every demotion cancel the
    # recognition that caused it, which is what oscillated.
    frame_cap_exit_gpu_pct: float = 90.0
    # Consecutive capped-looking readings before a cap is recognised and the
    # tier starts easing down; single readings right after frames resume still
    # describe the previous session.
    frame_cap_confirm_windows: int = 3
    # Frametime this much worse (percent) than at recognition also drops the
    # cap: the tier, not the cap, is the limit again.
    frame_cap_exit_pacing_pct: float = 15.0
    # Nothing is presenting AND the card is below this: not a game we cannot
    # measure, just a desktop. Far below the cap bars, because those describe a
    # card working under a limit while this one describes a card doing nothing.
    desktop_idle_gpu_pct: float = 20.0
    # How long the desktop has to stay that quiet before the tier eases down.
    desktop_idle_after_s: float = 60.0

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
            frame_cap_enter_gpu_pct=default.frame_cap_enter_gpu_pct,
            frame_cap_exit_gpu_pct=default.frame_cap_exit_gpu_pct,
            frame_cap_confirm_windows=default.frame_cap_confirm_windows,
            frame_cap_exit_pacing_pct=default.frame_cap_exit_pacing_pct,
            desktop_idle_gpu_pct=default.desktop_idle_gpu_pct,
            desktop_idle_after_s=default.desktop_idle_after_s,
        )
        return config.with_responsiveness(env).with_env_overrides(env)

    def with_responsiveness(
        self,
        env: Mapping[str, str] | None = None,
    ) -> "AdaptiveProfilePolicyConfig":
        """Scale every windows and dwell knob by the one-word preset.

        Cadence only: the utilisation bars and the pacing slack are
        judgements about a reading, not about how long to wait, and stay
        untouched. Applied before the per-knob overrides, so a knob the user
        set individually always wins over its scaled value.
        """
        values: Mapping[str, str] = os.environ if env is None else env
        raw = str(values.get(ADAPTIVE_RESPONSIVENESS_ENV) or "").strip().lower()
        factor = _RESPONSIVENESS_FACTORS.get(raw, 1.0)
        if factor == 1.0:
            return self

        def windows(count: int) -> int:
            return max(1, int(count * factor + 0.5))

        return replace(
            self,
            target_slow_windows=windows(self.target_slow_windows),
            near_slow_windows=windows(self.near_slow_windows),
            comfort_windows=windows(self.comfort_windows),
            performance_comfort_windows=windows(self.performance_comfort_windows),
            frame_cap_confirm_windows=windows(self.frame_cap_confirm_windows),
            demote_dwell_s=self.demote_dwell_s * factor,
            performance_demote_dwell_s=self.performance_demote_dwell_s * factor,
            desktop_idle_after_s=self.desktop_idle_after_s * factor,
        )

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
            frame_cap_enter_gpu_pct=_env_percentage(
                values,
                ADAPTIVE_FRAME_CAP_ENTER_GPU_PCT_ENV,
                self.frame_cap_enter_gpu_pct,
            ),
            frame_cap_exit_gpu_pct=_env_percentage(
                values,
                ADAPTIVE_FRAME_CAP_EXIT_GPU_PCT_ENV,
                self.frame_cap_exit_gpu_pct,
            ),
            frame_cap_confirm_windows=_env_int(
                values,
                ADAPTIVE_FRAME_CAP_CONFIRM_WINDOWS_ENV,
                self.frame_cap_confirm_windows,
            ),
            frame_cap_exit_pacing_pct=_env_percentage(
                values,
                ADAPTIVE_FRAME_CAP_EXIT_PACING_PCT_ENV,
                self.frame_cap_exit_pacing_pct,
            ),
            desktop_idle_gpu_pct=_env_percentage(
                values,
                ADAPTIVE_DESKTOP_IDLE_GPU_PCT_ENV,
                self.desktop_idle_gpu_pct,
            ),
            desktop_idle_after_s=_env_float(
                values,
                ADAPTIVE_DESKTOP_IDLE_AFTER_S_ENV,
                self.desktop_idle_after_s,
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
