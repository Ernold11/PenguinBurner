//! GPU mutation pipeline — port of
//! `runtime/gpu_control/vf_curve_runtime_policy.py`, `vf_curve_plan.py::apply_plan`,
//! and the profile power/memory apply in `runtime_auto_uv_profile.py`.
//!
//! Load-bearing ordering: persistence mode → memory offset (with read-back) →
//! per-point VF frequency offsets → power limit → locked clock ceiling.
//!
//! WHY the mem offset must precede the VF writes (still load-bearing): a coarse
//! mem-offset write WIPES the entire core per-point VF offset table — on BOTH
//! NVML API families (the modern `nvmlDeviceSetClockOffsets` and the deprecated
//! `nvmlDeviceSetMemClkVfOffset` fallback; each proven 2026-07-07 on the live
//! GPU, driver 610.43.02). The coupling is one-directional — VF writes leave the
//! mem offset untouched, and locked clocks / the power limit touch neither.
//! Applying the mem offset first, then the per-point offsets, leaves both live;
//! the reverse order silently erases the fresh curve.

use crate::gpu::GpuBackend;

use super::ceiling::FlattenedClockCeilingController;
use super::guard::select_expected_vf_samples;
use super::profile_store::{
    clamp_memory_offset_to_driver, load_auto_uv_final_curve, LoadedCurve, PlanItem,
    STOCK_PROFILE_SELECTOR,
};

/// `apply_plan`: MHz→kHz ×1000, then a single `SetControl` write. The engine
/// does the ×1000 here (coordinator answer, matches Python `apply_plan`).
pub fn apply_plan(backend: &dyn GpuBackend, plan: &[PlanItem]) -> crate::gpu::GpuResult<()> {
    let offsets: Vec<(u32, i32)> = plan
        .iter()
        .map(|p| (p.index, (p.new_offset_mhz * 1000) as i32))
        .collect();
    backend.apply_vf_offsets_khz(&offsets)
}

/// The applied base policy (`power_limit_w` / `mem_clk_vf_offset_mhz`).
#[derive(Debug, Clone, Copy, Default)]
pub struct GpuPolicyApplied {
    pub power_limit_w: Option<i64>,
    pub mem_clk_vf_offset_mhz: Option<i64>,
}

impl GpuPolicyApplied {
    /// `describe_translated_gpu_policy` for the two-key applied policy.
    pub fn describe(&self) -> String {
        let mut parts = Vec::new();
        if let Some(w) = self.power_limit_w {
            parts.push(format!("power-limit={w}W"));
        }
        if let Some(m) = self.mem_clk_vf_offset_mhz {
            parts.push(format!("mem-vf-offset={m:+}MHz"));
        }
        if parts.is_empty() {
            "none".to_string()
        } else {
            parts.join(", ")
        }
    }
}

#[derive(Default)]
pub struct VfPolicyResult<'a> {
    pub auto_uv_final_curve: Option<LoadedCurve>,
    pub vf_apply_plan: Option<Vec<PlanItem>>,
    pub active_vf_curve_source: Option<String>,
    pub auto_uv_profile_gpu_policy: GpuPolicyApplied,
    pub active_profile_id: String,
    pub active_profile_tier: String,
    pub active_profile_tier_key: String,
    pub clock_ceiling_controller: Option<FlattenedClockCeilingController<'a>>,
    pub vf_expected_samples: Vec<PlanItem>,
    /// Profile target (`lock_clock_mhz`, `candidate_voltage_mv`) for status lines.
    pub profile_clock_mhz: Option<f64>,
    pub profile_voltage_mv: Option<f64>,
}

fn apply_gpu_base_policy(
    backend: &dyn GpuBackend,
    enable_persistence_mode: bool,
    log: &mut dyn FnMut(&str),
) {
    if enable_persistence_mode {
        match backend.enable_persistence_mode() {
            Ok(()) => log("Enabled GPU persistence mode"),
            Err(exc) => log(&format!("Skipping GPU persistence mode: error={exc}")),
        }
    }
}

fn reset_gpu_to_stock(backend: &dyn GpuBackend, log: &mut dyn FnMut(&str)) {
    if let Err(exc) = backend.reset_locked_core_clocks() {
        log(&format!(
            "keep-stock: locked core clocks reset skipped: {exc}"
        ));
    }
    if let Err(exc) = backend.reset_locked_memory_clocks() {
        log(&format!(
            "keep-stock: locked memory clocks reset skipped: {exc}"
        ));
    }
    if let Err(exc) = backend.apply_clock_offsets(Some(0), Some(0)) {
        log(&format!("keep-stock: VF offset reset skipped: {exc}"));
    }
    let default_w = backend.query_power_limits().power_limit_default_w;
    if let Some(default_w) = default_w {
        if default_w != 0 {
            if let Err(exc) = backend.apply_power_limit_w(default_w) {
                log(&format!("keep-stock: power-limit reset skipped: {exc}"));
            }
        }
    }
    // Zero every editable point's freq offset (a default plan).
    let zero_offsets: Vec<(u32, i32)> = backend
        .editable_core_vf_points()
        .iter()
        .map(|p| (p.index, 0))
        .collect();
    if let Err(exc) = backend
        .refresh_vf_points()
        .and_then(|_| backend.apply_vf_offsets_khz(&zero_offsets))
    {
        log(&format!("keep-stock: V/F curve reset skipped: {exc}"));
    }
    log("keep-stock: GPU reset to factory V/F; no undervolt applied");
}

fn apply_power_limit(
    backend: &dyn GpuBackend,
    label: &str,
    power_limit_w: Option<i64>,
) -> Result<Option<i64>, String> {
    let Some(power) = power_limit_w.filter(|&p| p > 0) else {
        return Ok(None);
    };
    backend.apply_power_limit_w(power).map_err(|exc| {
        format!(
            "failed to apply saved profile power limit {power} W for {label}: driver rejected nvmlDeviceSetPowerManagementLimit: {exc}"
        )
    })?;
    Ok(Some(power))
}

fn apply_memory_offset(
    backend: &dyn GpuBackend,
    label: &str,
    memory_offset_mhz: Option<i64>,
) -> Result<Option<i64>, String> {
    let Some(raw) = memory_offset_mhz else {
        return Ok(None);
    };
    let driver_max = backend
        .memory_clock_offset_range_mhz()
        .map(|(_, max)| i64::from(max));
    let offset = clamp_memory_offset_to_driver(raw, driver_max);
    backend.apply_clock_offsets(None, Some(offset as i32)).map_err(|exc| {
        format!(
            "failed to apply saved profile memory offset {offset:+} MHz for {label}: driver rejected nvmlDeviceSetMemClkVfOffset: {exc}"
        )
    })?;
    Ok(Some(offset))
}

/// Apply the memory offset (when `memory_offset_mhz` is `Some`) and then the
/// per-point VF plan, re-reading the curve afterward. Returns the applied,
/// driver-clamped memory offset (`None` when `memory_offset_mhz` was `None`).
///
/// WHY the mem offset must precede the VF writes (load-bearing invariant): a
/// coarse mem-offset write WIPES the entire core per-point VF offset table — on
/// BOTH NVML API families (`nvmlDeviceSetClockOffsets` and the deprecated
/// `nvmlDeviceSetMemClkVfOffset` fallback; each proven 2026-07-07 on the live
/// GPU, driver 610.43.02). The coupling is one-directional — VF writes leave
/// the mem offset untouched, and locked clocks / the power limit touch
/// neither. Applying the mem offset first, then the per-point offsets, leaves
/// both live; the reverse order silently erases the fresh curve. Callers that
/// pass `None` (the startup and VF-reapply guards) have already
/// written/verified the mem offset just above the call, so the ordering still
/// holds.
pub fn apply_memory_offset_then_vf_plan(
    backend: &dyn GpuBackend,
    label: &str,
    memory_offset_mhz: Option<i64>,
    plan: &[PlanItem],
) -> Result<Option<i64>, String> {
    let memory = apply_memory_offset(backend, label, memory_offset_mhz)?;
    apply_plan(backend, plan).map_err(|e| e.to_string())?;
    backend.refresh_vf_points().map_err(|e| e.to_string())?;
    Ok(memory)
}

/// `configure_runtime_vf_curve_policy`. Fatal `Err` = initial power-limit /
/// memory-offset failure (spec 02 §6.4); everything else logs-and-continues.
pub fn configure_runtime_vf_curve_policy<'a>(
    backend: &'a dyn GpuBackend,
    enable_persistence_mode: bool,
    selector: &str,
    log: &mut dyn FnMut(&str),
) -> Result<VfPolicyResult<'a>, String> {
    let mut result = VfPolicyResult::default();

    if selector.trim() == STOCK_PROFILE_SELECTOR {
        apply_gpu_base_policy(backend, enable_persistence_mode, log);
        reset_gpu_to_stock(backend, log);
        result.active_vf_curve_source = Some("stock".to_string());
        return Ok(result);
    }

    match load_auto_uv_final_curve(selector) {
        Ok(curve) => result.auto_uv_final_curve = curve,
        Err(exc) => {
            log(&format!("Skipping auto-UV final curve: error={exc}"));
            result.auto_uv_final_curve = None;
        }
    }

    apply_gpu_base_policy(backend, enable_persistence_mode, log);

    if !backend.vf_curve_available() {
        return Ok(result);
    }

    apply_auto_uv_final_curve(&mut result, backend, log)?;
    Ok(result)
}

fn apply_auto_uv_final_curve<'a>(
    result: &mut VfPolicyResult<'a>,
    backend: &'a dyn GpuBackend,
    log: &mut dyn FnMut(&str),
) -> Result<(), String> {
    let Some(curve) = result.auto_uv_final_curve.clone() else {
        return Ok(());
    };

    // Memory offset FIRST — before the per-point VF writes (mem-wipes-VF; see
    // `apply_memory_offset_then_vf_plan`). Fatal on failure (spec 02 §6.4).
    // Recorded into the result immediately so the applied policy reflects hardware
    // even if the VF write below then fails and bails.
    let memory = apply_memory_offset(backend, "auto-UV final curve", curve.memory_offset_mhz)?;
    if let Some(m) = memory {
        log(&format!("Applied auto-UV profile memory offset: {m:+}MHz."));
    }
    result.auto_uv_profile_gpu_policy.mem_clk_vf_offset_mhz = memory;

    // Mem already applied above → pass None; the helper does the VF plan + readback.
    if let Err(exc) =
        apply_memory_offset_then_vf_plan(backend, "auto-UV final curve", None, &curve.plan)
    {
        log(&format!(
            "Skipping auto-UV final curve apply: path={} error={exc}",
            curve.path.display()
        ));
        result.auto_uv_final_curve = None;
        return Ok(());
    }

    result.vf_apply_plan = Some(curve.plan.clone());
    result.active_vf_curve_source = Some("auto-uv-final".to_string());
    result.active_profile_id = curve.profile_id.clone();
    result.active_profile_tier = curve.profile_tier.clone();
    result.active_profile_tier_key = curve.profile_tier_key.clone();
    result.vf_expected_samples = select_expected_vf_samples(&curve.plan, 8);
    result.profile_clock_mhz = Some(curve.lock_clock_mhz as f64);
    result.profile_voltage_mv = Some(curve.candidate_voltage_mv as f64);
    log(&format!(
        "Applied auto-UV final curve: path={} target={}MHz@{}mV points={}.",
        curve.path.display(),
        curve.lock_clock_mhz,
        curve.candidate_voltage_mv,
        curve.plan.len()
    ));

    let power = apply_power_limit(backend, "auto-UV final curve", curve.power_limit_w)?;
    if let Some(w) = power {
        log(&format!("Applied auto-UV profile power limit: {w}W."));
    }
    result.auto_uv_profile_gpu_policy.power_limit_w = power;

    let mut controller =
        FlattenedClockCeilingController::new(curve.flatten_target.clone(), backend);
    match controller.apply() {
        Ok(()) => {
            log(&format!(
                "Configured auto-UV clock ceiling: {}.",
                controller.describe()
            ));
            result.clock_ceiling_controller = Some(controller);
        }
        Err(exc) => {
            log(&format!(
                "Skipping auto-UV clock ceiling: path={} error={exc}",
                curve.path.display()
            ));
            result.clock_ceiling_controller = None;
        }
    }
    // keep the loaded curve on the result
    result.auto_uv_final_curve = Some(curve);
    Ok(())
}

/// The adaptive tier switch's GPU ops: memory offset → VF plan → power →
/// ceiling retarget. Returns the applied power/mem for the switch result. The
/// mem-before-VF ordering (mem write wipes the VF table) is owned by
/// `apply_memory_offset_then_vf_plan`; power stays after the VF writes.
pub fn apply_adaptive_curve(
    backend: &dyn GpuBackend,
    curve: &LoadedCurve,
    ceiling: &mut Option<FlattenedClockCeilingController<'_>>,
    tier_label: &str,
) -> Result<(Option<i64>, Option<i64>), String> {
    let label = format!("adaptive {tier_label} profile");
    let memory =
        apply_memory_offset_then_vf_plan(backend, &label, curve.memory_offset_mhz, &curve.plan)?;
    let power = apply_power_limit(backend, &label, curve.power_limit_w)?;
    if let Some(controller) = ceiling {
        controller
            .retarget(curve.flatten_target.clone())
            .map_err(|e| e.to_string())?;
    }
    Ok((power, memory))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::gpu::mock::{MockGpu, MockOp};

    fn item(index: u32, offset: i64) -> PlanItem {
        PlanItem {
            index,
            voltage_mv: 800 + offset,
            base_mhz: 2000,
            target_mhz: 2000 + offset,
            new_offset_mhz: offset,
        }
    }

    #[test]
    fn apply_plan_converts_mhz_to_khz_single_write() {
        let mock = MockGpu::new();
        let plan = vec![item(3, 120), item(7, -50)];
        apply_plan(&mock, &plan).unwrap();
        assert_eq!(
            mock.recorded(),
            vec![MockOp::ApplyVfOffsets {
                offsets: vec![(3, 120_000), (7, -50_000)]
            }]
        );
    }

    #[test]
    fn stock_reset_sequence_single_locked_core_reset() {
        let mut mock = MockGpu::new();
        mock.vf_available = true;
        mock.power_limits.power_limit_default_w = Some(300);
        mock.vf_points = vec![crate::gpu::VfPoint {
            index: 5,
            type_: 0,
            voltage_based: 1,
            ..Default::default()
        }];
        let mut log = |_: &str| {};
        let result = configure_runtime_vf_curve_policy(&mock, true, "__stock__", &mut log).unwrap();
        assert_eq!(result.active_vf_curve_source.as_deref(), Some("stock"));
        let ops = mock.recorded();
        // persistence → reset core (ONCE) → reset mem → offsets 0/0 → power default
        // → refresh (not recorded) → vf zero plan.
        assert_eq!(
            ops,
            vec![
                MockOp::EnablePersistence,
                MockOp::ResetLockedCoreClocks,
                MockOp::ResetLockedMemoryClocks,
                MockOp::ApplyClockOffsets {
                    gpc_mhz: Some(0),
                    mem_mhz: Some(0)
                },
                MockOp::ApplyPowerLimit { power_limit_w: 300 },
                MockOp::ApplyVfOffsets {
                    offsets: vec![(5, 0)]
                },
            ]
        );
    }

    #[test]
    fn power_limit_failure_is_fatal() {
        let mock = MockGpu::new();
        mock.inject_failure(
            "apply_power_limit_w",
            crate::gpu::GpuError::nvml_with_text(
                "nvmlDeviceSetPowerManagementLimit",
                3,
                "Not Supported",
            ),
        );
        let err = apply_power_limit(&mock, "auto-UV final curve", Some(320)).unwrap_err();
        assert_eq!(
            err,
            "failed to apply saved profile power limit 320 W for auto-UV final curve: driver rejected nvmlDeviceSetPowerManagementLimit: nvmlDeviceSetPowerManagementLimit failed with NVML error 3: Not Supported"
        );
    }

    #[test]
    fn memory_offset_clamped_to_driver_and_applied() {
        let mut mock = MockGpu::new();
        mock.mem_offset_range = Some((-2000, 3000));
        let applied = apply_memory_offset(&mock, "auto-UV final curve", Some(5000)).unwrap();
        assert_eq!(applied, Some(3000)); // clamped to driver max
        assert_eq!(
            mock.recorded(),
            vec![MockOp::ApplyClockOffsets {
                gpc_mhz: None,
                mem_mhz: Some(3000)
            }]
        );
    }

    /// The adaptive tier switch pins the same mem-before-VF ordering as the
    /// startup pipeline: a coarse mem-offset write (either NVML API family)
    /// wipes the whole core per-point VF offset table (mem-wipes-VF coupling),
    /// so the memory offset must land BEFORE the VF plan or the switch erases
    /// its own curve.
    /// Power limit stays after the VF writes.
    #[test]
    fn adaptive_curve_applies_mem_before_vf_then_power() {
        use super::super::profile_store::FlattenTarget;

        let mut mock = MockGpu::new();
        mock.mem_offset_range = Some((-2000, 6000));
        let curve = LoadedCurve {
            path: std::path::PathBuf::new(),
            profile_id: "adaptive-test".to_string(),
            profile_tier: "Balanced".to_string(),
            profile_tier_key: "balanced".to_string(),
            plan: vec![item(12, 240)],
            lock_clock_mhz: 2640,
            candidate_voltage_mv: 875,
            memory_offset_mhz: Some(1500),
            power_limit_w: Some(320),
            flatten_target: FlattenTarget {
                source: "auto-uv-final".to_string(),
                lock_clock_mhz: 2640,
                lock_voltage_mv: Some(875),
                end_voltage_mv: Some(875),
                tail_point_count: Some(1),
                ceiling_clock_mhz: None,
                tail_rise_bins: None,
            },
        };

        let mut ceiling = None;
        let (power, memory) =
            apply_adaptive_curve(&mock, &curve, &mut ceiling, "balanced").unwrap();
        assert_eq!(power, Some(320));
        assert_eq!(memory, Some(1500));
        // Exact op order: mem offset → VF plan → power limit (no ceiling here).
        assert_eq!(
            mock.recorded(),
            vec![
                MockOp::ApplyClockOffsets {
                    gpc_mhz: None,
                    mem_mhz: Some(1500)
                },
                MockOp::ApplyVfOffsets {
                    offsets: vec![(12, 240_000)]
                },
                MockOp::ApplyPowerLimit { power_limit_w: 320 },
            ]
        );
    }

    #[test]
    fn describe_policy() {
        let policy = GpuPolicyApplied {
            power_limit_w: Some(320),
            mem_clk_vf_offset_mhz: Some(1500),
        };
        assert_eq!(
            policy.describe(),
            "power-limit=320W, mem-vf-offset=+1500MHz"
        );
        assert_eq!(GpuPolicyApplied::default().describe(), "none");
    }
}
