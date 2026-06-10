from __future__ import annotations

from dataclasses import dataclass

from saved_uv_profiles.profile_tiers import (
    PROFILE_TIER_BALANCED,
    PROFILE_TIER_EFFICIENCY,
    PROFILE_TIER_PERFORMANCE,
    PROFILE_TIERS,
    normalize_profile_tier,
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
    performance_demote_dwell_s: float = 90.0


@dataclass(frozen=True, slots=True)
class AdaptiveProfileDecision:
    tier: str
    changed: bool
    reason: str


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

    def update(
        self,
        *,
        present_frametime_p95_ms: float | None,
        available_tiers: list[str] | tuple[str, ...],
        now_monotonic: float,
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
            return self._switch(
                ordered_tiers[-1],
                now_monotonic=float(now_monotonic),
                reason="badly-slow",
            )

        if frametime_ms > self.config.near_slow_ms:
            self._near_slow_count += 1
            self._target_slow_count = 0
            self._comfort_count = 0
            if self._near_slow_count >= int(self.config.near_slow_windows):
                return self._switch(
                    _higher_tier(self.current_tier, ordered_tiers),
                    now_monotonic=float(now_monotonic),
                    reason="clearly-slow",
                )
            return AdaptiveProfileDecision(tier=self.current_tier, changed=False, reason="clearly-slow-wait")

        if frametime_ms > self.config.target_ms:
            self._target_slow_count += 1
            self._near_slow_count = 0
            self._comfort_count = 0
            if self._target_slow_count >= int(self.config.target_slow_windows):
                return self._switch(
                    _higher_tier(self.current_tier, ordered_tiers),
                    now_monotonic=float(now_monotonic),
                    reason="near-slow",
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
