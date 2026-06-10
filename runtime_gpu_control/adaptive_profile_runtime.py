from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from runtime_debug import log as runtime_log
from saved_uv_profiles import (
    available_adaptive_tiers,
    load_auto_uv_final_curve,
    read_auto_uv_profiles,
    resolve_profile_tier_profiles,
)
from saved_uv_profiles.profile_tiers import profile_tier_label
from saved_uv_profiles.runtime_auto_uv_profile import apply_auto_uv_profile_memory_offset

from .adaptive_profile_policy import AdaptiveProfileController
from .vf_curve_reset_guard import select_expected_vf_samples


@dataclass(slots=True)
class AdaptiveAutoUvRuntimeDependencies:
    read_auto_uv_profiles: Callable = read_auto_uv_profiles
    resolve_profile_tier_profiles: Callable = resolve_profile_tier_profiles
    available_adaptive_tiers: Callable = available_adaptive_tiers
    load_auto_uv_final_curve: Callable = load_auto_uv_final_curve
    apply_plan: Callable | None = None
    apply_memory_offset: Callable = apply_auto_uv_profile_memory_offset
    select_expected_vf_samples: Callable = select_expected_vf_samples
    log: Callable[[str], None] = runtime_log


@dataclass(slots=True)
class AdaptiveAutoUvSwitchResult:
    changed: bool
    tier: str
    reason: str
    vf_apply_result: dict | None = None
    vf_expected_samples: list = field(default_factory=list)
    memory_offset_mhz: int | None = None


class AdaptiveAutoUvRuntimeController:
    def __init__(
        self,
        *,
        current_tier: str,
        vf_curve_reader,
        gpu_policy_controller,
        clock_ceiling_controller=None,
        overlay_state_publisher=None,
        dependencies: AdaptiveAutoUvRuntimeDependencies | None = None,
    ) -> None:
        self.deps = dependencies or AdaptiveAutoUvRuntimeDependencies()
        self.vf_curve_reader = vf_curve_reader
        self.gpu_policy_controller = gpu_policy_controller
        self.clock_ceiling_controller = clock_ceiling_controller
        self.overlay_state_publisher = overlay_state_publisher
        self.tier_curves = self._load_tier_curves()
        self.available_tiers = [
            tier
            for tier in self.deps.available_adaptive_tiers(self.tier_curves)
            if tier in self.tier_curves
        ]
        initial_tier = current_tier or (
            self.available_tiers[0] if self.available_tiers else ""
        )
        self.policy = AdaptiveProfileController(initial_tier=initial_tier)
        if len(self.available_tiers) >= 2:
            labels = ", ".join(
                profile_tier_label(tier) for tier in self.available_tiers
            )
            self.deps.log(f"Adaptive Auto-UV enabled for tiers: {labels}.")
        else:
            self.deps.log(
                "Adaptive Auto-UV disabled: fewer than two profile tiers are available."
            )

    @property
    def enabled(self) -> bool:
        return len(self.available_tiers) >= 2 and self.vf_curve_reader is not None

    def update(
        self,
        *,
        latency_snapshot: dict | None,
        now_monotonic: float,
    ) -> AdaptiveAutoUvSwitchResult | None:
        if not self.enabled:
            return None
        present_ms = None
        if isinstance(latency_snapshot, dict):
            present_ms = latency_snapshot.get("base_present_frametime_p95_ms")
        decision = self.policy.update(
            present_frametime_p95_ms=present_ms,
            available_tiers=self.available_tiers,
            now_monotonic=float(now_monotonic),
        )
        if not decision.changed:
            return AdaptiveAutoUvSwitchResult(
                changed=False,
                tier=decision.tier,
                reason=decision.reason,
            )
        curve = self.tier_curves.get(decision.tier)
        if curve is None:
            return AdaptiveAutoUvSwitchResult(
                changed=False,
                tier=decision.tier,
                reason="missing-curve",
            )
        return self._apply_curve(decision.tier, curve, reason=decision.reason)

    def _load_tier_curves(self) -> dict[str, dict]:
        profiles = self.deps.read_auto_uv_profiles()
        resolved = self.deps.resolve_profile_tier_profiles(profiles)
        curves: dict[str, dict] = {}
        for tier, profile in resolved.items():
            if not isinstance(profile, dict):
                continue
            profile_id = str(profile.get("profile_id") or "").strip()
            if not profile_id:
                continue
            try:
                curve = self.deps.load_auto_uv_final_curve(profile_id)
            except Exception as exc:
                self.deps.log(
                    f"Adaptive Auto-UV skipped {profile_tier_label(tier)} tier: {exc}"
                )
                continue
            if isinstance(curve, dict):
                curves[tier] = curve
        return curves

    def _apply_curve(
        self,
        tier: str,
        curve: dict,
        *,
        reason: str,
    ) -> AdaptiveAutoUvSwitchResult:
        apply_plan = self.deps.apply_plan
        if apply_plan is None:
            from afterburner.import_vf_curve import apply_plan as default_apply_plan

            apply_plan = default_apply_plan
        apply_plan(self.vf_curve_reader, curve["plan"])
        self.vf_curve_reader.refresh_points()
        memory_policy = self.deps.apply_memory_offset(
            profile_label=f"adaptive {profile_tier_label(tier)} profile",
            memory_offset_mhz=curve.get("memory_offset_mhz"),
            gpu_policy_controller=self.gpu_policy_controller,
        )
        if self.clock_ceiling_controller is not None:
            self.clock_ceiling_controller.retarget(curve["flatten_target"])
        if self.overlay_state_publisher is not None:
            self.overlay_state_publisher.profile_tier = str(
                curve.get("profile_tier") or ""
            )
            self.overlay_state_publisher.profile_tier_key = str(
                curve.get("profile_tier_key") or tier
            )
            self.overlay_state_publisher.profile_id = str(curve.get("profile_id") or "")
        path = Path(str(curve.get("path") or ""))
        self.deps.log(
            "Adaptive Auto-UV switched profile: "
            f"tier={profile_tier_label(tier)} reason={reason} path={path}"
        )
        vf_apply_result = {
            "source": "auto-uv-final",
            "plan": curve["plan"],
            "path": curve["path"],
        }
        return AdaptiveAutoUvSwitchResult(
            changed=True,
            tier=tier,
            reason=reason,
            vf_apply_result=vf_apply_result,
            vf_expected_samples=self.deps.select_expected_vf_samples(curve["plan"]),
            memory_offset_mhz=(
                memory_policy.get("mem_clk_vf_offset_mhz")
                if isinstance(memory_policy, dict)
                else None
            ),
        )
