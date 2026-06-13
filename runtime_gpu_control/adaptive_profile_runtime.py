from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from runtime_support.adaptive_target_fps import (
    adaptive_target_fps_from_config,
    adaptive_target_fps_from_env,
    format_adaptive_target_fps,
    parse_adaptive_target_fps,
)
from runtime_support.runtime_debug import log as runtime_log
from saved_uv_profiles import (
    available_adaptive_tiers,
    load_auto_uv_final_curve,
    read_auto_uv_profiles,
    resolve_profile_tier_profiles,
)
from saved_uv_profiles.profile_tiers import profile_tier_label
from saved_uv_profiles.runtime_auto_uv_profile import apply_auto_uv_profile_memory_offset

from .adaptive_profile_policy import AdaptiveProfileController, AdaptiveProfilePolicyConfig
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
    adaptive_target_fps_from_config: Callable = adaptive_target_fps_from_config
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
        adaptive_target_fps: object | None = None,
        runtime_config_path: str | Path | None = None,
        dependencies: AdaptiveAutoUvRuntimeDependencies | None = None,
    ) -> None:
        self.deps = dependencies or AdaptiveAutoUvRuntimeDependencies()
        self.vf_curve_reader = vf_curve_reader
        self.gpu_policy_controller = gpu_policy_controller
        self.clock_ceiling_controller = clock_ceiling_controller
        self.overlay_state_publisher = overlay_state_publisher
        self.runtime_config_path = (
            Path(runtime_config_path).expanduser()
            if runtime_config_path is not None
            else None
        )
        self.tier_curves = self._load_tier_curves()
        self.available_tiers = [
            tier
            for tier in self.deps.available_adaptive_tiers(self.tier_curves)
            if tier in self.tier_curves
        ]
        initial_tier = current_tier or (
            self.available_tiers[0] if self.available_tiers else ""
        )
        self.adaptive_target_fps = (
            adaptive_target_fps_from_env()
            if adaptive_target_fps is None
            else parse_adaptive_target_fps(adaptive_target_fps)
        )
        policy_config = AdaptiveProfilePolicyConfig.for_target_fps(
            self.adaptive_target_fps
        )
        self.policy = AdaptiveProfileController(
            initial_tier=initial_tier,
            config=policy_config,
        )
        self._last_cpu_bound_guard_log_monotonic = -1_000_000_000.0
        if len(self.available_tiers) >= 2:
            labels = ", ".join(
                profile_tier_label(tier) for tier in self.available_tiers
            )
            self.deps.log(f"Adaptive Auto-UV enabled for tiers: {labels}.")
            self.deps.log(
                "Adaptive Auto-UV target: "
                f"{format_adaptive_target_fps(self.adaptive_target_fps)} FPS "
                f"({policy_config.target_ms:.2f} ms p95)."
            )
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
        self._refresh_adaptive_target_fps()
        present_ms = None
        if isinstance(latency_snapshot, dict):
            present_ms = latency_snapshot.get("base_present_frametime_p95_ms")
        policy_state = self.policy.snapshot_state()
        gpu_util_pct = _publisher_metric(
            self.overlay_state_publisher,
            "last_gpu_util_pct",
        )
        cpu_util_pct = _publisher_metric(
            self.overlay_state_publisher,
            "last_cpu_util_pct",
        )
        cpu_peak_thread_pct = _publisher_metric(
            self.overlay_state_publisher,
            "last_cpu_peak_thread_pct",
        )
        decision = self.policy.update(
            present_frametime_p95_ms=present_ms,
            available_tiers=self.available_tiers,
            now_monotonic=float(now_monotonic),
            gpu_util_pct=gpu_util_pct,
            cpu_util_pct=cpu_util_pct,
            cpu_peak_thread_pct=cpu_peak_thread_pct,
        )
        if not decision.changed:
            self._maybe_log_cpu_bound_guard(
                decision=decision,
                now_monotonic=float(now_monotonic),
                gpu_util_pct=gpu_util_pct,
                cpu_util_pct=cpu_util_pct,
                cpu_peak_thread_pct=cpu_peak_thread_pct,
            )
            return AdaptiveAutoUvSwitchResult(
                changed=False,
                tier=decision.tier,
                reason=decision.reason,
            )
        curve = self.tier_curves.get(decision.tier)
        if curve is None:
            self.policy.restore_state(policy_state)
            return AdaptiveAutoUvSwitchResult(
                changed=False,
                tier=policy_state.current_tier,
                reason="missing-curve",
            )
        try:
            return self._apply_curve(decision.tier, curve, reason=decision.reason)
        except Exception as exc:
            self.policy.restore_state(policy_state)
            self.deps.log(
                "Adaptive Auto-UV switch failed: "
                f"tier={profile_tier_label(decision.tier)} "
                f"reason={decision.reason} error={exc}"
            )
            return AdaptiveAutoUvSwitchResult(
                changed=False,
                tier=policy_state.current_tier,
                reason="apply-failed",
            )

    def _refresh_adaptive_target_fps(self) -> None:
        if self.runtime_config_path is None:
            return
        try:
            target_fps = self.deps.adaptive_target_fps_from_config(
                self.runtime_config_path,
                default=self.adaptive_target_fps,
            )
        except Exception:
            return
        target_fps = parse_adaptive_target_fps(
            target_fps,
            default=self.adaptive_target_fps,
        )
        if target_fps == self.adaptive_target_fps:
            return
        self.adaptive_target_fps = target_fps
        policy_config = AdaptiveProfilePolicyConfig.for_target_fps(target_fps)
        self.policy.reconfigure(policy_config)
        self.deps.log(
            "Adaptive Auto-UV target updated: "
            f"{format_adaptive_target_fps(target_fps)} FPS "
            f"({policy_config.target_ms:.2f} ms p95)."
        )

    def _maybe_log_cpu_bound_guard(
        self,
        *,
        decision: object,
        now_monotonic: float,
        gpu_util_pct: object,
        cpu_util_pct: object,
        cpu_peak_thread_pct: object,
    ) -> None:
        if getattr(decision, "reason", "") != "cpu-bound-performance-block":
            return
        if (
            float(now_monotonic) - self._last_cpu_bound_guard_log_monotonic
            < 30.0
        ):
            return
        self._last_cpu_bound_guard_log_monotonic = float(now_monotonic)
        self.deps.log(
            "Adaptive Auto-UV held below Performance: "
            "reason=cpu-bound "
            f"gpu={_metric_text(gpu_util_pct)}% "
            f"cpu={_metric_text(cpu_util_pct)}% "
            f"cpu-t={_metric_text(cpu_peak_thread_pct)}%."
        )

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


def _publisher_metric(publisher: object | None, name: str) -> object | None:
    if publisher is None:
        return None
    return getattr(publisher, name, None)


def _metric_text(value: object) -> str:
    if value is None:
        return "n/a"
    try:
        return str(int(round(float(value))))
    except (TypeError, ValueError):
        return "n/a"
