from __future__ import annotations

from dataclasses import dataclass, replace
import os

from runtime.support.adaptive_target_fps import adaptive_target_ms_from_fps
from profiles.uv.profile_tiers import (
    PROFILE_TIER_BALANCED,
    PROFILE_TIER_PERFORMANCE,
    PROFILE_TIERS,
    normalize_profile_tier,
)

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
    cpu_bound_gpu_util_max_pct: float = 85.0
    cpu_bound_peak_thread_min_pct: float = 70.0
    cpu_bound_process_util_min_pct: float = 12.0

    @classmethod
    def for_target_fps(
        cls,
        fps: object,
        *,
        env: dict[str, str] | None = None,
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
        env: dict[str, str] | None = None,
    ) -> "AdaptiveProfilePolicyConfig":
        env = os.environ if env is None else env
        return replace(
            self,
            target_slow_windows=_env_int(
                env,
                ADAPTIVE_TARGET_SLOW_WINDOWS_ENV,
                self.target_slow_windows,
            ),
            near_slow_windows=_env_int(
                env,
                ADAPTIVE_NEAR_SLOW_WINDOWS_ENV,
                self.near_slow_windows,
            ),
            comfort_windows=_env_int(
                env,
                ADAPTIVE_COMFORT_WINDOWS_ENV,
                self.comfort_windows,
            ),
            performance_comfort_windows=_env_int(
                env,
                ADAPTIVE_PERFORMANCE_COMFORT_WINDOWS_ENV,
                self.performance_comfort_windows,
            ),
            demote_dwell_s=_env_float(
                env,
                ADAPTIVE_DEMOTE_DWELL_S_ENV,
                self.demote_dwell_s,
            ),
            performance_demote_dwell_s=_env_float(
                env,
                ADAPTIVE_PERFORMANCE_DEMOTE_DWELL_S_ENV,
                self.performance_demote_dwell_s,
            ),
            cpu_bound_gpu_util_max_pct=_env_float(
                env,
                ADAPTIVE_CPU_BOUND_GPU_UTIL_MAX_ENV,
                self.cpu_bound_gpu_util_max_pct,
            ),
            cpu_bound_peak_thread_min_pct=_env_float(
                env,
                ADAPTIVE_CPU_BOUND_PEAK_THREAD_MIN_ENV,
                self.cpu_bound_peak_thread_min_pct,
            ),
            cpu_bound_process_util_min_pct=_env_float(
                env,
                ADAPTIVE_CPU_BOUND_PROCESS_UTIL_MIN_ENV,
                self.cpu_bound_process_util_min_pct,
            ),
        )


@dataclass(frozen=True, slots=True)
class AdaptiveProfileDecision:
    tier: str
    changed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class AdaptiveProfilePolicyState:
    current_tier: str
    last_switch_monotonic: float
    target_slow_count: int
    near_slow_count: int
    comfort_count: int


class AdaptiveProfileController:
    def __init__(
        self,
        *,
        initial_tier: object | None = PROFILE_TIER_BALANCED,
        config: AdaptiveProfilePolicyConfig | None = None,
    ) -> None:
        self.config = config or AdaptiveProfilePolicyConfig()
        self.current_tier = normalize_profile_tier(
            initial_tier,
            default=PROFILE_TIER_BALANCED,
        )
        self.last_switch_monotonic = 0.0
        self._target_slow_count = 0
        self._near_slow_count = 0
        self._comfort_count = 0

    def snapshot_state(self) -> AdaptiveProfilePolicyState:
        return AdaptiveProfilePolicyState(
            current_tier=self.current_tier,
            last_switch_monotonic=self.last_switch_monotonic,
            target_slow_count=self._target_slow_count,
            near_slow_count=self._near_slow_count,
            comfort_count=self._comfort_count,
        )

    def restore_state(self, state: AdaptiveProfilePolicyState) -> None:
        self.current_tier = state.current_tier
        self.last_switch_monotonic = float(state.last_switch_monotonic)
        self._target_slow_count = int(state.target_slow_count)
        self._near_slow_count = int(state.near_slow_count)
        self._comfort_count = int(state.comfort_count)

    def reconfigure(self, config: AdaptiveProfilePolicyConfig) -> None:
        self.config = config
        self._reset_counts()

    def update(
        self,
        *,
        present_frametime_p95_ms: float | None,
        available_tiers: list[str] | tuple[str, ...],
        now_monotonic: float,
        gpu_util_pct: float | None = None,
        cpu_util_pct: float | None = None,
        cpu_peak_thread_pct: float | None = None,
    ) -> AdaptiveProfileDecision:
        ordered_tiers = _ordered_available_tiers(available_tiers)
        if len(ordered_tiers) < 2:
            tier = ordered_tiers[0] if ordered_tiers else self.current_tier
            self.current_tier = tier
            self._reset_counts()
            return AdaptiveProfileDecision(tier=tier, changed=False, reason="not-enough-tiers")

        if self.current_tier not in ordered_tiers:
            self.current_tier = ordered_tiers[0]
            self.last_switch_monotonic = float(now_monotonic)
            self._reset_counts()
            return AdaptiveProfileDecision(tier=self.current_tier, changed=True, reason="snap-to-available")

        if present_frametime_p95_ms is None:
            self._reset_counts()
            return AdaptiveProfileDecision(tier=self.current_tier, changed=False, reason="no-sample")

        frametime_ms = float(present_frametime_p95_ms)
        if frametime_ms > self.config.badly_slow_ms:
            return self._switch_with_cpu_bound_guard(
                ordered_tiers[-1],
                ordered_tiers=ordered_tiers,
                now_monotonic=float(now_monotonic),
                reason="badly-slow",
                gpu_util_pct=gpu_util_pct,
                cpu_util_pct=cpu_util_pct,
                cpu_peak_thread_pct=cpu_peak_thread_pct,
            )

        if frametime_ms > self.config.near_slow_ms:
            self._near_slow_count += 1
            self._target_slow_count = 0
            self._comfort_count = 0
            if self._near_slow_count >= int(self.config.near_slow_windows):
                return self._switch_with_cpu_bound_guard(
                    _higher_tier(self.current_tier, ordered_tiers),
                    ordered_tiers=ordered_tiers,
                    now_monotonic=float(now_monotonic),
                    reason="clearly-slow",
                    gpu_util_pct=gpu_util_pct,
                    cpu_util_pct=cpu_util_pct,
                    cpu_peak_thread_pct=cpu_peak_thread_pct,
                )
            return AdaptiveProfileDecision(tier=self.current_tier, changed=False, reason="clearly-slow-wait")

        if frametime_ms > self.config.target_ms:
            self._target_slow_count += 1
            self._near_slow_count = 0
            self._comfort_count = 0
            if self._target_slow_count >= int(self.config.target_slow_windows):
                return self._switch_with_cpu_bound_guard(
                    _higher_tier(self.current_tier, ordered_tiers),
                    ordered_tiers=ordered_tiers,
                    now_monotonic=float(now_monotonic),
                    reason="near-slow",
                    gpu_util_pct=gpu_util_pct,
                    cpu_util_pct=cpu_util_pct,
                    cpu_peak_thread_pct=cpu_peak_thread_pct,
                )
            return AdaptiveProfileDecision(tier=self.current_tier, changed=False, reason="near-slow-wait")

        if frametime_ms <= self.config.comfort_ms:
            self._target_slow_count = 0
            self._near_slow_count = 0
            self._comfort_count += 1
            required_windows = (
                int(self.config.performance_comfort_windows)
                if self.current_tier == PROFILE_TIER_PERFORMANCE
                else int(self.config.comfort_windows)
            )
            required_dwell = (
                float(self.config.performance_demote_dwell_s)
                if self.current_tier == PROFILE_TIER_PERFORMANCE
                else float(self.config.demote_dwell_s)
            )
            dwell_s = float(now_monotonic) - float(self.last_switch_monotonic)
            if self._comfort_count >= required_windows and dwell_s >= required_dwell:
                return self._switch(
                    _lower_tier(self.current_tier, ordered_tiers),
                    now_monotonic=float(now_monotonic),
                    reason="comfort",
                )
            return AdaptiveProfileDecision(tier=self.current_tier, changed=False, reason="comfort-wait")

        self._reset_counts()
        return AdaptiveProfileDecision(tier=self.current_tier, changed=False, reason="target-ok")

    def _switch_with_cpu_bound_guard(
        self,
        target_tier: str,
        *,
        ordered_tiers: list[str],
        now_monotonic: float,
        reason: str,
        gpu_util_pct: object,
        cpu_util_pct: object,
        cpu_peak_thread_pct: object,
    ) -> AdaptiveProfileDecision:
        target_tier = normalize_profile_tier(target_tier, default=self.current_tier)
        if not self._performance_promotion_cpu_bound(
            target_tier,
            gpu_util_pct=gpu_util_pct,
            cpu_util_pct=cpu_util_pct,
            cpu_peak_thread_pct=cpu_peak_thread_pct,
        ):
            return self._switch(
                target_tier,
                now_monotonic=now_monotonic,
                reason=reason,
            )

        capped_tier = _highest_non_performance_tier(ordered_tiers)
        if _tier_index(capped_tier, ordered_tiers) > _tier_index(
            self.current_tier,
            ordered_tiers,
        ):
            return self._switch(
                capped_tier,
                now_monotonic=now_monotonic,
                reason="cpu-bound-performance-cap",
            )

        self._reset_counts()
        return AdaptiveProfileDecision(
            tier=self.current_tier,
            changed=False,
            reason="cpu-bound-performance-block",
        )

    def _performance_promotion_cpu_bound(
        self,
        target_tier: str,
        *,
        gpu_util_pct: object,
        cpu_util_pct: object,
        cpu_peak_thread_pct: object,
    ) -> bool:
        if normalize_profile_tier(target_tier) != PROFILE_TIER_PERFORMANCE:
            return False

        gpu_value = _float_or_none(gpu_util_pct)
        if gpu_value is None:
            return False
        if gpu_value > float(self.config.cpu_bound_gpu_util_max_pct):
            return False

        peak_thread_value = _float_or_none(cpu_peak_thread_pct)
        process_value = _float_or_none(cpu_util_pct)
        peak_thread_busy = (
            peak_thread_value is not None
            and peak_thread_value >= float(self.config.cpu_bound_peak_thread_min_pct)
        )
        process_busy = (
            process_value is not None
            and process_value >= float(self.config.cpu_bound_process_util_min_pct)
        )
        return peak_thread_busy or process_busy

    def _switch(
        self,
        target_tier: str,
        *,
        now_monotonic: float,
        reason: str,
    ) -> AdaptiveProfileDecision:
        target_tier = normalize_profile_tier(target_tier, default=self.current_tier)
        if target_tier == self.current_tier:
            self._reset_counts()
            return AdaptiveProfileDecision(tier=self.current_tier, changed=False, reason=f"{reason}-already")
        self.current_tier = target_tier
        self.last_switch_monotonic = float(now_monotonic)
        self._reset_counts()
        return AdaptiveProfileDecision(tier=self.current_tier, changed=True, reason=reason)

    def _reset_counts(self) -> None:
        self._target_slow_count = 0
        self._near_slow_count = 0
        self._comfort_count = 0


def _ordered_available_tiers(raw_tiers: list[str] | tuple[str, ...]) -> list[str]:
    normalized = {normalize_profile_tier(tier) for tier in raw_tiers}
    return [tier for tier in PROFILE_TIERS if tier in normalized]


def _higher_tier(current_tier: str, ordered_tiers: list[str]) -> str:
    index = ordered_tiers.index(current_tier)
    return ordered_tiers[min(len(ordered_tiers) - 1, index + 1)]


def _lower_tier(current_tier: str, ordered_tiers: list[str]) -> str:
    index = ordered_tiers.index(current_tier)
    return ordered_tiers[max(0, index - 1)]


def _highest_non_performance_tier(ordered_tiers: list[str]) -> str:
    for tier in reversed(ordered_tiers):
        if tier != PROFILE_TIER_PERFORMANCE:
            return tier
    return ordered_tiers[0] if ordered_tiers else PROFILE_TIER_BALANCED


def _tier_index(tier: str, ordered_tiers: list[str]) -> int:
    try:
        return ordered_tiers.index(tier)
    except ValueError:
        return -1


def _float_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _env_int(env: dict[str, str], name: str, default: int) -> int:
    raw = str(env.get(name) or "").strip()
    if not raw:
        return int(default)
    try:
        value = int(raw)
    except ValueError:
        return int(default)
    if value < 1 or value > 120:
        return int(default)
    return value


def _env_float(env: dict[str, str], name: str, default: float) -> float:
    raw = str(env.get(name) or "").strip()
    if not raw:
        return float(default)
    try:
        value = float(raw)
    except ValueError:
        return float(default)
    if value < 0.0 or value > 3600.0:
        return float(default)
    return value
