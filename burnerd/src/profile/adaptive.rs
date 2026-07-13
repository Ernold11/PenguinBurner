//! Adaptive Auto-UV tier policy + runtime controller — port of
//! `runtime/gpu_control/adaptive_profile_policy.py` and
//! `adaptive_profile_runtime.py`. The pure policy is a frametime-p95 state
//! machine with a CPU-bound promotion guard; the runtime controller loads
//! per-tier curves and applies switches to the GPU.

use std::collections::HashMap;

use crate::gpu::GpuBackend;

use super::apply::apply_adaptive_curve;
use super::ceiling::FlattenedClockCeilingController;
use super::guard::select_expected_vf_samples;
use super::logfmt::tier_switch_line;
use super::runtime_spec::{
    normalize_profile_tier, profile_tier_label, AdaptivePolicySpec, LoadedCurve, PlanItem,
    PROFILE_TIERS, PROFILE_TIER_BALANCED, PROFILE_TIER_PERFORMANCE,
};
use super::telemetry::OverlayStatePublisher;
use super::LatencySnapshot;

const CPU_BOUND_GUARD_LOG_THROTTLE_S: f64 = 30.0;

// --- policy config ----------------------------------------------------------

#[derive(Debug, Clone, Copy)]
pub struct PolicyConfig {
    pub comfort_ms: f64,
    pub target_ms: f64,
    pub near_slow_ms: f64,
    pub badly_slow_ms: f64,
    pub target_slow_windows: i64,
    pub near_slow_windows: i64,
    pub comfort_windows: i64,
    pub performance_comfort_windows: i64,
    pub demote_dwell_s: f64,
    pub performance_demote_dwell_s: f64,
    pub cpu_bound_gpu_util_max_pct: f64,
    pub cpu_bound_peak_thread_min_pct: f64,
    pub cpu_bound_process_util_min_pct: f64,
}

impl Default for PolicyConfig {
    fn default() -> Self {
        PolicyConfig {
            comfort_ms: 14.5,
            target_ms: 16.6,
            near_slow_ms: 18.5,
            badly_slow_ms: 22.0,
            target_slow_windows: 3,
            near_slow_windows: 2,
            comfort_windows: 6,
            performance_comfort_windows: 10,
            demote_dwell_s: 60.0,
            performance_demote_dwell_s: 45.0,
            cpu_bound_gpu_util_max_pct: 60.0,
            cpu_bound_peak_thread_min_pct: 97.0,
            cpu_bound_process_util_min_pct: 60.0,
        }
    }
}

impl PolicyConfig {
    pub fn for_target_fps(fps: f64) -> Self {
        let default = PolicyConfig::default();
        let target_ms = 1000.0 / fps;
        let scale = target_ms / default.target_ms;
        PolicyConfig {
            comfort_ms: default.comfort_ms * scale,
            target_ms,
            near_slow_ms: default.near_slow_ms * scale,
            badly_slow_ms: default.badly_slow_ms * scale,
            ..default
        }
    }

    fn from_spec(spec: &AdaptivePolicySpec) -> Self {
        let scaled = Self::for_target_fps(spec.target_fps);
        PolicyConfig {
            target_slow_windows: spec.target_slow_windows,
            near_slow_windows: spec.near_slow_windows,
            comfort_windows: spec.comfort_windows,
            performance_comfort_windows: spec.performance_comfort_windows,
            demote_dwell_s: spec.demote_dwell_s,
            performance_demote_dwell_s: spec.performance_demote_dwell_s,
            cpu_bound_gpu_util_max_pct: spec.cpu_bound_gpu_util_max_pct,
            cpu_bound_peak_thread_min_pct: spec.cpu_bound_peak_thread_min_pct,
            cpu_bound_process_util_min_pct: spec.cpu_bound_process_util_min_pct,
            ..scaled
        }
    }
}

// --- pure policy controller -------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Decision {
    pub tier: String,
    pub changed: bool,
    pub reason: String,
}

#[derive(Debug, Clone)]
struct PolicyState {
    current_tier: String,
    last_switch_monotonic: f64,
    target_slow_count: i64,
    near_slow_count: i64,
    comfort_count: i64,
}

/// How far back the cpu-bound guard looks when judging utilization.
const CPU_BOUND_WINDOW_S: f64 = 8.0;

#[derive(Debug, Clone, Copy)]
struct UtilSample {
    at: f64,
    gpu: Option<f64>,
    cpu: Option<f64>,
    peak_thread: Option<f64>,
}

pub struct AdaptiveProfileController {
    config: PolicyConfig,
    state: PolicyState,
    util_samples: std::collections::VecDeque<UtilSample>,
}

fn ordered_available_tiers(raw: &[String]) -> Vec<String> {
    let normalized: Vec<String> = raw.iter().map(|t| normalize_profile_tier(t, "")).collect();
    PROFILE_TIERS
        .iter()
        .filter(|tier| normalized.iter().any(|n| n == *tier))
        .map(|t| t.to_string())
        .collect()
}

fn higher_tier(current: &str, ordered: &[String]) -> String {
    let idx = ordered.iter().position(|t| t == current).unwrap_or(0);
    ordered[(idx + 1).min(ordered.len() - 1)].clone()
}

fn lower_tier(current: &str, ordered: &[String]) -> String {
    let idx = ordered.iter().position(|t| t == current).unwrap_or(0);
    ordered[idx.saturating_sub(1)].clone()
}

fn highest_non_performance_tier(ordered: &[String]) -> String {
    for tier in ordered.iter().rev() {
        if tier != PROFILE_TIER_PERFORMANCE {
            return tier.clone();
        }
    }
    ordered
        .first()
        .cloned()
        .unwrap_or_else(|| PROFILE_TIER_BALANCED.to_string())
}

fn tier_index(tier: &str, ordered: &[String]) -> i64 {
    ordered
        .iter()
        .position(|t| t == tier)
        .map_or(-1, |i| i as i64)
}

impl AdaptiveProfileController {
    pub fn new(initial_tier: &str, config: PolicyConfig) -> Self {
        AdaptiveProfileController {
            config,
            state: PolicyState {
                current_tier: normalize_profile_tier(initial_tier, PROFILE_TIER_BALANCED),
                last_switch_monotonic: 0.0,
                target_slow_count: 0,
                near_slow_count: 0,
                comfort_count: 0,
            },
            util_samples: std::collections::VecDeque::new(),
        }
    }

    fn snapshot_state(&self) -> PolicyState {
        self.state.clone()
    }

    fn restore_state(&mut self, state: &PolicyState) {
        self.state = state.clone();
    }

    fn reset_counts(&mut self) {
        self.state.target_slow_count = 0;
        self.state.near_slow_count = 0;
        self.state.comfort_count = 0;
    }

    #[allow(clippy::too_many_arguments)]
    pub fn update(
        &mut self,
        present_frametime_p95_ms: Option<f64>,
        available_tiers: &[String],
        now_monotonic: f64,
        gpu_util_pct: Option<f64>,
        cpu_util_pct: Option<f64>,
        cpu_peak_thread_pct: Option<f64>,
    ) -> Decision {
        self.record_util_sample(
            now_monotonic,
            gpu_util_pct,
            cpu_util_pct,
            cpu_peak_thread_pct,
        );
        let ordered = ordered_available_tiers(available_tiers);
        if ordered.len() < 2 {
            let tier = ordered
                .first()
                .cloned()
                .unwrap_or_else(|| self.state.current_tier.clone());
            self.state.current_tier = tier.clone();
            self.reset_counts();
            return Decision {
                tier,
                changed: false,
                reason: "not-enough-tiers".into(),
            };
        }
        if !ordered.contains(&self.state.current_tier) {
            self.state.current_tier = ordered[0].clone();
            self.state.last_switch_monotonic = now_monotonic;
            self.reset_counts();
            return Decision {
                tier: self.state.current_tier.clone(),
                changed: true,
                reason: "snap-to-available".into(),
            };
        }
        let Some(frametime_ms) = present_frametime_p95_ms else {
            self.reset_counts();
            return Decision {
                tier: self.state.current_tier.clone(),
                changed: false,
                reason: "no-sample".into(),
            };
        };

        if frametime_ms > self.config.badly_slow_ms {
            return self.switch_with_cpu_bound_guard(
                ordered[ordered.len() - 1].clone(),
                &ordered,
                now_monotonic,
                "badly-slow",
                gpu_util_pct,
                cpu_util_pct,
                cpu_peak_thread_pct,
            );
        }
        if frametime_ms > self.config.near_slow_ms {
            self.state.near_slow_count += 1;
            self.state.target_slow_count = 0;
            self.state.comfort_count = 0;
            if self.state.near_slow_count >= self.config.near_slow_windows {
                let target = higher_tier(&self.state.current_tier, &ordered);
                return self.switch_with_cpu_bound_guard(
                    target,
                    &ordered,
                    now_monotonic,
                    "clearly-slow",
                    gpu_util_pct,
                    cpu_util_pct,
                    cpu_peak_thread_pct,
                );
            }
            return Decision {
                tier: self.state.current_tier.clone(),
                changed: false,
                reason: "clearly-slow-wait".into(),
            };
        }
        if frametime_ms > self.config.target_ms {
            self.state.target_slow_count += 1;
            self.state.near_slow_count = 0;
            self.state.comfort_count = 0;
            if self.state.target_slow_count >= self.config.target_slow_windows {
                let target = higher_tier(&self.state.current_tier, &ordered);
                return self.switch_with_cpu_bound_guard(
                    target,
                    &ordered,
                    now_monotonic,
                    "near-slow",
                    gpu_util_pct,
                    cpu_util_pct,
                    cpu_peak_thread_pct,
                );
            }
            return Decision {
                tier: self.state.current_tier.clone(),
                changed: false,
                reason: "near-slow-wait".into(),
            };
        }
        if frametime_ms <= self.config.comfort_ms {
            self.state.target_slow_count = 0;
            self.state.near_slow_count = 0;
            self.state.comfort_count += 1;
            let required_windows = if self.state.current_tier == PROFILE_TIER_PERFORMANCE {
                self.config.performance_comfort_windows
            } else {
                self.config.comfort_windows
            };
            let required_dwell = if self.state.current_tier == PROFILE_TIER_PERFORMANCE {
                self.config.performance_demote_dwell_s
            } else {
                self.config.demote_dwell_s
            };
            let dwell_s = now_monotonic - self.state.last_switch_monotonic;
            if self.state.comfort_count >= required_windows && dwell_s >= required_dwell {
                let target = lower_tier(&self.state.current_tier, &ordered);
                return self.switch(target, now_monotonic, "comfort");
            }
            return Decision {
                tier: self.state.current_tier.clone(),
                changed: false,
                reason: "comfort-wait".into(),
            };
        }
        self.reset_counts();
        Decision {
            tier: self.state.current_tier.clone(),
            changed: false,
            reason: "target-ok".into(),
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn switch_with_cpu_bound_guard(
        &mut self,
        target_tier: String,
        ordered: &[String],
        now_monotonic: f64,
        reason: &str,
        gpu_util_pct: Option<f64>,
        cpu_util_pct: Option<f64>,
        cpu_peak_thread_pct: Option<f64>,
    ) -> Decision {
        let target_tier = normalize_profile_tier(&target_tier, &self.state.current_tier);
        if !self.performance_promotion_cpu_bound(
            &target_tier,
            gpu_util_pct,
            cpu_util_pct,
            cpu_peak_thread_pct,
        ) {
            return self.switch(target_tier, now_monotonic, reason);
        }
        let capped = highest_non_performance_tier(ordered);
        if tier_index(&capped, ordered) > tier_index(&self.state.current_tier, ordered) {
            return self.switch(capped, now_monotonic, "cpu-bound-performance-cap");
        }
        self.reset_counts();
        Decision {
            tier: self.state.current_tier.clone(),
            changed: false,
            reason: "cpu-bound-performance-block".into(),
        }
    }

    fn record_util_sample(
        &mut self,
        now_monotonic: f64,
        gpu_util_pct: Option<f64>,
        cpu_util_pct: Option<f64>,
        cpu_peak_thread_pct: Option<f64>,
    ) {
        self.util_samples.push_back(UtilSample {
            at: now_monotonic,
            gpu: gpu_util_pct,
            cpu: cpu_util_pct,
            peak_thread: cpu_peak_thread_pct,
        });
        while let Some(front) = self.util_samples.front() {
            if now_monotonic - front.at > CPU_BOUND_WINDOW_S {
                self.util_samples.pop_front();
            } else {
                break;
            }
        }
    }

    fn windowed_avg(&self, pick: impl Fn(&UtilSample) -> Option<f64>) -> Option<f64> {
        let values: Vec<f64> = self.util_samples.iter().filter_map(&pick).collect();
        if values.is_empty() {
            return None;
        }
        Some(values.iter().sum::<f64>() / values.len() as f64)
    }

    fn performance_promotion_cpu_bound(
        &self,
        target_tier: &str,
        gpu_util_pct: Option<f64>,
        cpu_util_pct: Option<f64>,
        cpu_peak_thread_pct: Option<f64>,
    ) -> bool {
        if normalize_profile_tier(target_tier, "") != PROFILE_TIER_PERFORMANCE {
            return false;
        }
        // Judge the last few seconds, not the instant the promotion fires: a
        // menu shader-compile spike (or one calm tick right after it) must
        // not decide a gameplay tier. Falls back to the instantaneous values
        // while the window is still filling.
        let gpu_avg = self.windowed_avg(|s| s.gpu).or(gpu_util_pct);
        let cpu_avg = self.windowed_avg(|s| s.cpu).or(cpu_util_pct);
        let peak_avg = self.windowed_avg(|s| s.peak_thread).or(cpu_peak_thread_pct);
        let Some(gpu) = gpu_avg else {
            return false;
        };
        if gpu > self.config.cpu_bound_gpu_util_max_pct {
            return false;
        }
        let peak_busy =
            peak_avg.is_some_and(|v| v >= self.config.cpu_bound_peak_thread_min_pct);
        let process_busy =
            cpu_avg.is_some_and(|v| v >= self.config.cpu_bound_process_util_min_pct);
        peak_busy || process_busy
    }

    fn switch(&mut self, target_tier: String, now_monotonic: f64, reason: &str) -> Decision {
        let target_tier = normalize_profile_tier(&target_tier, &self.state.current_tier);
        if target_tier == self.state.current_tier {
            self.reset_counts();
            return Decision {
                tier: self.state.current_tier.clone(),
                changed: false,
                reason: format!("{reason}-already"),
            };
        }
        self.state.current_tier = target_tier.clone();
        self.state.last_switch_monotonic = now_monotonic;
        self.reset_counts();
        Decision {
            tier: target_tier,
            changed: true,
            reason: reason.to_string(),
        }
    }
}

// --- runtime controller -----------------------------------------------------

pub struct AdaptiveSwitchResult {
    pub changed: bool,
    pub tier: String,
    pub vf_apply_plan: Option<Vec<PlanItem>>,
    pub vf_expected_samples: Vec<PlanItem>,
    pub memory_offset_mhz: Option<i64>,
}

pub struct AdaptiveAutoUvRuntimeController {
    policy: AdaptiveProfileController,
    tier_curves: HashMap<String, LoadedCurve>,
    available_tiers: Vec<String>,
    last_tier_label: Option<String>,
    last_cpu_bound_guard_log: f64,
    vf_curve_available: bool,
}

impl AdaptiveAutoUvRuntimeController {
    /// Construct + enumerate tier curves. `log` receives the enable/target lines.
    pub fn new(
        current_tier: &str,
        vf_curve_available: bool,
        tier_curves: HashMap<String, LoadedCurve>,
        policy_spec: &AdaptivePolicySpec,
        log: &mut dyn FnMut(&str),
    ) -> Self {
        // available tiers: PROFILE_TIERS order, deduped by profile_id.
        let mut available_tiers = Vec::new();
        let mut seen: Vec<String> = Vec::new();
        for tier in PROFILE_TIERS {
            let Some(curve) = tier_curves.get(tier) else {
                continue;
            };
            let pid = curve.profile_id.trim().to_string();
            if pid.is_empty() || seen.contains(&pid) {
                continue;
            }
            available_tiers.push(tier.to_string());
            seen.push(pid);
        }

        let initial_tier = if !current_tier.is_empty() {
            current_tier.to_string()
        } else {
            available_tiers.first().cloned().unwrap_or_default()
        };
        let adaptive_target_fps = policy_spec.target_fps;
        let policy_config = PolicyConfig::from_spec(policy_spec);
        let policy = AdaptiveProfileController::new(&initial_tier, policy_config);
        let last_tier_label = if initial_tier.is_empty() {
            None
        } else {
            Some(profile_tier_label(&initial_tier))
        };

        if available_tiers.len() >= 2 {
            let labels = available_tiers
                .iter()
                .map(|t| profile_tier_label(t))
                .collect::<Vec<_>>()
                .join(", ");
            log(&format!("Adaptive Auto-UV enabled for tiers: {labels}."));
            log(&format!(
                "Adaptive Auto-UV target: {} FPS ({:.2} ms p95).",
                format_target_fps(adaptive_target_fps),
                policy_config.target_ms
            ));
        } else if available_tiers.len() == 1 {
            log(&format!(
                "Adaptive Auto-UV: single profile tier available ({}); applying it without tier switching.",
                profile_tier_label(&available_tiers[0])
            ));
        } else {
            log("Adaptive Auto-UV disabled: no profile tiers are available.");
        }

        AdaptiveAutoUvRuntimeController {
            policy,
            tier_curves,
            available_tiers,
            last_tier_label,
            last_cpu_bound_guard_log: -1_000_000_000.0,
            vf_curve_available,
        }
    }

    pub fn enabled(&self) -> bool {
        self.available_tiers.len() >= 2 && self.vf_curve_available
    }

    #[allow(clippy::too_many_arguments)]
    pub fn update(
        &mut self,
        latency_snapshot: Option<&LatencySnapshot>,
        now_monotonic: f64,
        backend: &dyn GpuBackend,
        ceiling: &mut Option<FlattenedClockCeilingController<'_>>,
        publisher: &mut OverlayStatePublisher,
        log: &mut dyn FnMut(&str),
    ) -> Option<AdaptiveSwitchResult> {
        if !self.enabled() {
            return None;
        }
        let present_ms = latency_snapshot.and_then(|s| s.base_present_frametime_p95_ms);
        let policy_state = self.policy.snapshot_state();
        let gpu = publisher.last_gpu_util_pct().map(|v| v as f64);
        let cpu = publisher.last_cpu_util_pct().map(|v| v as f64);
        let cpu_peak = publisher.last_cpu_peak_thread_pct().map(|v| v as f64);
        let decision = self.policy.update(
            present_ms,
            &self.available_tiers,
            now_monotonic,
            gpu,
            cpu,
            cpu_peak,
        );
        if !decision.changed {
            self.maybe_log_cpu_bound_guard(&decision, now_monotonic, gpu, cpu, cpu_peak, log);
            return Some(AdaptiveSwitchResult {
                changed: false,
                tier: decision.tier,
                vf_apply_plan: None,
                vf_expected_samples: Vec::new(),
                memory_offset_mhz: None,
            });
        }
        let Some(curve) = self.tier_curves.get(&decision.tier).cloned() else {
            self.policy.restore_state(&policy_state);
            return Some(AdaptiveSwitchResult {
                changed: false,
                tier: policy_state.current_tier,
                vf_apply_plan: None,
                vf_expected_samples: Vec::new(),
                memory_offset_mhz: None,
            });
        };
        match self.apply_curve(
            &decision.tier,
            &curve,
            &decision.reason,
            backend,
            ceiling,
            publisher,
            log,
        ) {
            Ok(result) => Some(result),
            Err(exc) => {
                self.policy.restore_state(&policy_state);
                log(&format!(
                    "Adaptive Auto-UV switch failed: tier={} reason={} error={exc}",
                    profile_tier_label(&decision.tier),
                    decision.reason
                ));
                Some(AdaptiveSwitchResult {
                    changed: false,
                    tier: policy_state.current_tier,
                    vf_apply_plan: None,
                    vf_expected_samples: Vec::new(),
                    memory_offset_mhz: None,
                })
            }
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn apply_curve(
        &mut self,
        tier: &str,
        curve: &LoadedCurve,
        reason: &str,
        backend: &dyn GpuBackend,
        ceiling: &mut Option<FlattenedClockCeilingController<'_>>,
        publisher: &mut OverlayStatePublisher,
        log: &mut dyn FnMut(&str),
    ) -> Result<AdaptiveSwitchResult, String> {
        let label = profile_tier_label(tier);
        let (_power, memory) = apply_adaptive_curve(backend, curve, ceiling, &label)?;
        publisher.profile_tier = curve.profile_tier.clone();
        publisher.profile_tier_key = if curve.profile_tier_key.is_empty() {
            tier.to_string()
        } else {
            curve.profile_tier_key.clone()
        };
        publisher.profile_id = curve.profile_id.clone();
        log(&tier_switch_line(
            self.last_tier_label.as_deref().unwrap_or("?"),
            &label,
            reason,
        ));
        self.last_tier_label = Some(label);
        Ok(AdaptiveSwitchResult {
            changed: true,
            tier: tier.to_string(),
            vf_apply_plan: Some(curve.plan.clone()),
            vf_expected_samples: select_expected_vf_samples(&curve.plan, 8),
            memory_offset_mhz: memory,
        })
    }

    fn maybe_log_cpu_bound_guard(
        &mut self,
        decision: &Decision,
        now_monotonic: f64,
        gpu: Option<f64>,
        cpu: Option<f64>,
        cpu_peak: Option<f64>,
        log: &mut dyn FnMut(&str),
    ) {
        if decision.reason != "cpu-bound-performance-block" {
            return;
        }
        if now_monotonic - self.last_cpu_bound_guard_log < CPU_BOUND_GUARD_LOG_THROTTLE_S {
            return;
        }
        self.last_cpu_bound_guard_log = now_monotonic;
        log(&format!(
            "Adaptive Auto-UV held below Performance: reason=cpu-bound gpu={}% cpu={}% cpu-t={}%.",
            metric_text(gpu),
            metric_text(cpu),
            metric_text(cpu_peak)
        ));
    }
}

fn metric_text(value: Option<f64>) -> String {
    match value {
        Some(v) => (super::round_half_even(v) as i64).to_string(),
        None => "n/a".to_string(),
    }
}

fn format_target_fps(fps: f64) -> String {
    if fps.fract().abs() < f64::EPSILON {
        format!("{fps:.0}")
    } else {
        format!("{fps:.3}")
            .trim_end_matches('0')
            .trim_end_matches('.')
            .to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tiers() -> Vec<String> {
        vec!["efficiency".into(), "balanced".into(), "performance".into()]
    }

    fn controller() -> AdaptiveProfileController {
        AdaptiveProfileController::new("balanced", PolicyConfig::default())
    }

    #[test]
    fn badly_slow_jumps_to_top_tier() {
        let mut c = controller();
        let d = c.update(Some(30.0), &tiers(), 100.0, None, None, None);
        assert!(d.changed);
        assert_eq!(d.tier, "performance");
        assert_eq!(d.reason, "badly-slow");
    }

    #[test]
    fn near_slow_promotes_after_windows() {
        let mut c = controller();
        // target_ms 16.6, near_slow_ms 18.5. frametime 17 → target-slow band.
        // Need target_slow_windows=3 samples to promote.
        let d1 = c.update(Some(17.0), &tiers(), 1.0, None, None, None);
        assert_eq!(d1.reason, "near-slow-wait");
        let d2 = c.update(Some(17.0), &tiers(), 2.0, None, None, None);
        assert_eq!(d2.reason, "near-slow-wait");
        let d3 = c.update(Some(17.0), &tiers(), 3.0, None, None, None);
        assert!(d3.changed);
        assert_eq!(d3.tier, "performance");
        assert_eq!(d3.reason, "near-slow");
    }

    #[test]
    fn comfort_demotes_after_windows_and_dwell() {
        let mut c = controller();
        c.state.current_tier = "performance".into();
        c.state.last_switch_monotonic = 0.0;
        // comfort_ms 14.5; performance requires 10 windows + 45s dwell.
        for i in 1..10 {
            let d = c.update(Some(10.0), &tiers(), i as f64, None, None, None);
            assert_eq!(d.reason, "comfort-wait");
        }
        let d = c.update(Some(10.0), &tiers(), 50.0, None, None, None);
        assert!(d.changed);
        assert_eq!(d.tier, "balanced");
        assert_eq!(d.reason, "comfort");
    }

    #[test]
    fn cpu_bound_window_forgets_shader_compile_spike() {
        // Menu shader compilation (GPU idle, one thread pegged) must not
        // poison the gameplay decision once the utilization window has
        // moved on: after ~8s of real GPU-bound gameplay samples the
        // promotion goes through.
        let mut c = controller();
        for i in 0..4 {
            let t = 100.0 + i as f64 * 2.0;
            let d = c.update(Some(30.0), &tiers(), t, Some(5.0), Some(80.0), Some(99.0));
            assert_ne!(d.tier, "performance", "shader-compile spike must hold");
        }
        // Gameplay: GPU pegged, CPU relaxed. Old samples age out of the
        // 8s window; the badly-slow promotion is then allowed.
        let mut promoted = false;
        for i in 0..8 {
            let t = 108.0 + i as f64 * 2.0;
            let d = c.update(Some(30.0), &tiers(), t, Some(92.0), Some(30.0), Some(60.0));
            if d.tier == "performance" {
                promoted = true;
                break;
            }
        }
        assert!(promoted, "gameplay window must lift the cpu-bound hold");
    }

    #[test]
    fn cpu_bound_blocks_performance_promotion() {
        let mut c = controller();
        c.state.current_tier = "balanced".into();
        // badly-slow would jump to performance, but an idle GPU with a truly
        // pegged thread caps it. (Thresholds are deliberately conservative:
        // gpu <= 60, thread >= 97 — a render thread at 70-90% is normal for
        // GPU-bound gameplay and must NOT hold the promotion.)
        let d = c.update(
            Some(30.0),
            &tiers(),
            1.0,
            Some(20.0),
            Some(30.0),
            Some(99.0),
        );
        assert!(!d.changed);
        assert_eq!(d.reason, "cpu-bound-performance-block");
    }

    /// Scripted frametime sequence; expected (tier, changed, reason) tuples
    /// generated by running the Python policy directly:
    ///   python3 -c 'from runtime.gpu_control.adaptive_profile_policy import
    ///   AdaptiveProfileController, AdaptiveProfilePolicyConfig; ...'
    ///   → [(balanced,F,near-slow-wait),(balanced,F,near-slow-wait),
    ///      (performance,T,near-slow),(performance,F,comfort-wait)]
    #[test]
    fn adaptive_sequence_matches_python() {
        let mut c = controller();
        let seq = [(17.0, 1.0), (17.0, 2.0), (17.0, 3.0), (10.0, 4.0)];
        let expected = [
            ("balanced", false, "near-slow-wait"),
            ("balanced", false, "near-slow-wait"),
            ("performance", true, "near-slow"),
            ("performance", false, "comfort-wait"),
        ];
        for (i, (ft, now)) in seq.iter().enumerate() {
            let d = c.update(Some(*ft), &tiers(), *now, None, None, None);
            assert_eq!(
                (d.tier.as_str(), d.changed, d.reason.as_str()),
                expected[i],
                "step {i}"
            );
        }
    }

    #[test]
    fn no_sample_holds_tier() {
        let mut c = controller();
        let d = c.update(None, &tiers(), 1.0, None, None, None);
        assert!(!d.changed);
        assert_eq!(d.reason, "no-sample");
    }

    #[test]
    fn runtime_spec_policy_values_are_used() {
        let spec = AdaptivePolicySpec {
            target_fps: 60.0,
            target_slow_windows: 2,
            near_slow_windows: 4,
            comfort_windows: 7,
            performance_comfort_windows: 11,
            demote_dwell_s: 50.0,
            performance_demote_dwell_s: 40.0,
            cpu_bound_gpu_util_max_pct: 80.0,
            cpu_bound_peak_thread_min_pct: 75.0,
            cpu_bound_process_util_min_pct: 15.0,
        };
        let config = PolicyConfig::from_spec(&spec);
        assert_eq!(config.target_slow_windows, 2);
        assert_eq!(config.near_slow_windows, 4);
        assert_eq!(config.comfort_windows, 7);
        assert_eq!(config.performance_comfort_windows, 11);
        assert_eq!(config.demote_dwell_s, 50.0);
        assert_eq!(config.cpu_bound_gpu_util_max_pct, 80.0);
    }
}
