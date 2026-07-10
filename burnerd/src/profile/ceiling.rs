//! Flattened clock-ceiling controller — port of
//! `runtime/gpu_control/flattened_clock_ceiling.py`. Hides exact-lock vs
//! range-ceiling driver calls from the loop; `retarget` swaps on a tier switch.

use super::runtime_spec::FlattenTarget;
use crate::gpu::GpuBackend;

/// `describe_afterburner_dynamic_lock` (integrations/afterburner/vfcurve_describe.py).
pub fn describe_afterburner_dynamic_lock(target: &FlattenTarget) -> String {
    let mut parts = vec![
        format!(
            "source={}",
            if target.source.is_empty() {
                "unknown"
            } else {
                &target.source
            }
        ),
        format!("lock={}MHz", target.lock_clock_mhz),
    ];
    if let Some(v) = target.lock_voltage_mv {
        let last = parts.last_mut().unwrap();
        last.push_str(&format!("@{v}mV"));
    }
    if let (Some(lv), Some(ev)) = (target.lock_voltage_mv, target.end_voltage_mv) {
        parts.push(format!("tail={lv}-{ev}mV"));
    }
    if let Some(count) = target.tail_point_count {
        if count != 0 {
            parts.push(format!("points={count}"));
        }
    }
    parts.join(", ")
}

#[derive(Debug, Clone)]
struct RangeLock {
    requested_max_clock_mhz: i64,
    applied_min_clock_mhz: i64,
    applied_max_clock_mhz: i64,
    max_mode: String,
}

pub struct FlattenedClockCeilingController<'a> {
    flatten_target: FlattenTarget,
    backend: &'a dyn GpuBackend,
    exact_lock: bool,
    active: bool,
    range_lock: Option<RangeLock>,
}

impl<'a> FlattenedClockCeilingController<'a> {
    /// Default `exact_lock=false` (range ceiling), matching the Python ctor.
    pub fn new(flatten_target: FlattenTarget, backend: &'a dyn GpuBackend) -> Self {
        FlattenedClockCeilingController {
            flatten_target,
            backend,
            exact_lock: false,
            active: false,
            range_lock: None,
        }
    }

    fn target_clock_mhz(&self) -> i64 {
        if self.exact_lock {
            self.flatten_target.lock_clock_mhz
        } else {
            self.flatten_target
                .ceiling_clock_mhz
                .unwrap_or(self.flatten_target.lock_clock_mhz)
        }
    }

    fn requested_max_clock_mhz(&self) -> i64 {
        self.range_lock
            .as_ref()
            .map_or_else(|| self.target_clock_mhz(), |r| r.requested_max_clock_mhz)
    }

    fn applied_max_clock_mhz(&self) -> i64 {
        self.range_lock
            .as_ref()
            .map_or_else(|| self.target_clock_mhz(), |r| r.applied_max_clock_mhz)
    }

    fn applied_min_clock_mhz(&self) -> i64 {
        self.range_lock
            .as_ref()
            .map_or_else(|| self.target_clock_mhz(), |r| r.applied_min_clock_mhz)
    }

    pub fn apply(&mut self) -> crate::gpu::GpuResult<()> {
        let target = self.target_clock_mhz();
        if self.exact_lock {
            let snap = self
                .backend
                .apply_locked_core_clock_mhz(target, true, true)?;
            self.range_lock = Some(RangeLock {
                requested_max_clock_mhz: target,
                applied_min_clock_mhz: snap.applied_clock_mhz,
                applied_max_clock_mhz: snap.applied_clock_mhz,
                max_mode: snap.mode,
            });
        } else {
            let supported = self.backend.supported_core_clock_steps_mhz();
            let requested_min = supported.first().map_or(target, |&s| i64::from(s));
            let range = self.backend.apply_locked_core_clock_range_mhz(
                requested_min,
                target,
                true,
                true,
            )?;
            self.range_lock = Some(RangeLock {
                requested_max_clock_mhz: range.requested_max_clock_mhz,
                applied_min_clock_mhz: range.applied_min_clock_mhz,
                applied_max_clock_mhz: range.applied_max_clock_mhz,
                max_mode: range.max_mode,
            });
        }
        self.active = true;
        Ok(())
    }

    /// Release the lock (the only reset on shutdown). Errors are surfaced to the
    /// caller, which log-swallows them during cleanup so fans still restore.
    pub fn close(&mut self) -> crate::gpu::GpuResult<()> {
        if self.active {
            // Clear `active` only AFTER the reset succeeds (Python parity): a
            // failed reset leaves a live lock, and marking it inactive would
            // hide it from telemetry and make a later close() a silent no-op.
            self.backend.reset_locked_core_clocks()?;
            self.active = false;
        }
        Ok(())
    }

    pub fn retarget(&mut self, flatten_target: FlattenTarget) -> crate::gpu::GpuResult<()> {
        self.close()?;
        self.flatten_target = flatten_target;
        self.apply()
    }

    /// The per-poll telemetry fragment (`clk_ceiling=…MHz@…mV ` with a trailing
    /// space), empty when inactive.
    pub fn telemetry_text(&self) -> String {
        if !self.active {
            return String::new();
        }
        let (req, applied) = (self.requested_max_clock_mhz(), self.applied_max_clock_mhz());
        let mut clock_text = if req == applied {
            format!("{req}MHz")
        } else {
            format!("{req}->{applied}MHz")
        };
        if let Some(v) = self.flatten_target.lock_voltage_mv {
            clock_text.push_str(&format!("@{v}mV"));
        }
        let field = if self.exact_lock {
            "clk_lock"
        } else {
            "clk_ceiling"
        };
        format!("{field}={clock_text} ")
    }

    /// The human `Clock ceiling policy:` description.
    pub fn describe(&self) -> String {
        let (req, applied) = (self.requested_max_clock_mhz(), self.applied_max_clock_mhz());
        let snap_text = if self.range_lock.is_some() && req != applied {
            let mode = self
                .range_lock
                .as_ref()
                .map(|r| r.max_mode.as_str())
                .unwrap_or("");
            format!(", supported-clock={applied}MHz ({mode})")
        } else {
            String::new()
        };
        let min_text = if self.range_lock.is_some() && !self.exact_lock {
            format!(", min-step={}MHz", self.applied_min_clock_mhz())
        } else {
            String::new()
        };
        let policy_text = if self.exact_lock { "lock" } else { "ceiling" };
        format!(
            "{}, {}={}MHz{}{}",
            describe_afterburner_dynamic_lock(&self.flatten_target),
            policy_text,
            req,
            snap_text,
            min_text
        )
    }
}

/// Safety net for the early engine-setup error paths: those `?`-returns fire
/// before the cleanup guard takes ownership of the controller, and without this
/// the GPU would stay range-locked with nothing left to release it. `close()`
/// is idempotent (guards on `active`), so the explicit cleanup paths double up
/// harmlessly.
impl Drop for FlattenedClockCeilingController<'_> {
    fn drop(&mut self) {
        let _ = self.close();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::gpu::mock::MockGpu;
    use crate::gpu::mock::MockOp;

    fn target() -> FlattenTarget {
        FlattenTarget {
            source: "auto-uv-final".into(),
            lock_clock_mhz: 2640,
            lock_voltage_mv: Some(875),
            end_voltage_mv: Some(1050),
            tail_point_count: Some(3),
            ceiling_clock_mhz: None,
            tail_rise_bins: None,
        }
    }

    #[test]
    fn describe_dynamic_lock() {
        assert_eq!(
            describe_afterburner_dynamic_lock(&target()),
            "source=auto-uv-final, lock=2640MHz@875mV, tail=875-1050mV, points=3"
        );
    }

    #[test]
    fn range_apply_records_range_op_and_telemetry() {
        let mut mock = MockGpu::new();
        mock.supported_core_clocks = vec![2400, 2500, 2600, 2640, 2700];
        let mut ctrl = FlattenedClockCeilingController::new(target(), &mock);
        ctrl.apply().unwrap();
        // min from supported[0]=2400, max target 2640.
        let ops = mock.recorded();
        assert!(matches!(
            ops[0],
            MockOp::ApplyLockedCoreClockRange {
                min_mhz: 2400,
                max_mhz: 2640,
                prefer_max_not_above: true,
                snap: true,
            }
        ));
        assert_eq!(ctrl.telemetry_text(), "clk_ceiling=2640MHz@875mV ");
        ctrl.close().unwrap();
        assert_eq!(mock.recorded().last(), Some(&MockOp::ResetLockedCoreClocks));
        assert_eq!(ctrl.telemetry_text(), "");
    }

    /// A failed reset must leave the lock marked active (telemetry keeps
    /// reporting it, a later close() retries) — clearing `active` first would
    /// hide a live lock behind an inactive controller.
    #[test]
    fn close_failure_keeps_lock_active_until_reset_succeeds() {
        let mut mock = MockGpu::new();
        mock.supported_core_clocks = vec![2400, 2500, 2600, 2640, 2700];
        let mut ctrl = FlattenedClockCeilingController::new(target(), &mock);
        ctrl.apply().unwrap();

        mock.inject_failure(
            "reset_locked_core_clocks",
            crate::gpu::GpuError::nvml_with_text("nvmlDeviceResetGpuLockedClocks", 3, "Unknown"),
        );
        assert!(ctrl.close().is_err());
        // Still active: the lock is live and must not be reported as released.
        assert_eq!(ctrl.telemetry_text(), "clk_ceiling=2640MHz@875mV ");

        mock.clear_failure("reset_locked_core_clocks");
        ctrl.close().unwrap();
        assert_eq!(ctrl.telemetry_text(), "");
        assert_eq!(mock.recorded().last(), Some(&MockOp::ResetLockedCoreClocks));
    }
}
