//! System suspend/resume detection and post-resume state recovery.
//!
//! After S3/s2idle the driver can silently lose or corrupt applied tuning
//! (power limits stuck at wrong values, locked clocks reset, wiped VF
//! tables). The engine loop already re-verifies the VF curve; this module
//! adds the detection edge and the power-limit re-verification so the whole
//! profile is re-asserted shortly after every resume.
//!
//! Detection is deliberately dependency-free: `CLOCK_MONOTONIC` stops while
//! the system sleeps, `CLOCK_BOOTTIME` keeps counting, so a tick-over-tick
//! divergence between the two IS a completed suspend cycle — no
//! D-Bus/logind subscription, works on any init system, and also catches
//! sleeps initiated outside logind. A wedged NVML call cannot false-positive:
//! both clocks advance identically while the system is awake.

use crate::gpu::GpuBackend;

/// Divergence below this is clock jitter; above it, a real sleep. Ticks are
/// seconds apart and both clocks have nanosecond resolution, so anything in
/// between is unambiguous.
pub const SLEEP_GAP_THRESHOLD_S: f64 = 5.0;
/// Wait after detection before touching the GPU: the driver may still be
/// re-initializing right after resume (readback can be transient garbage).
pub const RESUME_REAPPLY_GRACE_S: f64 = 3.0;
/// Re-verification attempts before the engine stops retrying. Giving up is
/// logged loudly but non-fatal: a dying engine would strip fan control and
/// the clock ceiling while deliberately leaving the undervolt applied — a
/// strictly worse state than continuing with the guards that re-verify every
/// tick.
pub const RESUME_REAPPLY_MAX_ATTEMPTS: u32 = 3;

/// Seconds from `CLOCK_BOOTTIME`: like `CLOCK_MONOTONIC` but it keeps
/// counting across system suspend. Shares the sampling helper with
/// `monotonic_now` so the gap computation compares like with like.
pub fn boottime_now() -> f64 {
    super::clock_seconds(libc::CLOCK_BOOTTIME)
}

pub struct SleepGapDetector {
    threshold_s: f64,
    last_monotonic: f64,
    last_boottime: f64,
}

impl SleepGapDetector {
    pub fn new(threshold_s: f64, monotonic: f64, boottime: f64) -> Self {
        Self {
            threshold_s,
            last_monotonic: monotonic,
            last_boottime: boottime,
        }
    }

    /// Feed one tick's clock pair; returns the slept seconds when the ticks
    /// straddled a suspend. Baselines always advance, so one sleep is
    /// reported exactly once.
    pub fn observe(&mut self, monotonic: f64, boottime: f64) -> Option<f64> {
        let gap = (boottime - self.last_boottime) - (monotonic - self.last_monotonic);
        self.last_monotonic = monotonic;
        self.last_boottime = boottime;
        (gap > self.threshold_s).then_some(gap)
    }
}

/// Re-verify the applied power limit after a resume: read-first, and reapply
/// through the canonical `apply::apply_power_limit` (same NOT_SUPPORTED
/// tolerance and readback acceptance as startup) only when the management
/// limit no longer matches. The skip check deliberately ignores
/// `enforced_power_limit_w`: the enforced value is the minimum over all
/// transient constraints (thermal/idle caps) and can coincidentally equal
/// the profile value while the management limit was reset. `Ok(true)` means
/// a drifted limit was reapplied.
pub fn verify_power_limit(
    backend: &dyn GpuBackend,
    expected_w: Option<i64>,
    log: &mut dyn FnMut(&str),
) -> Result<bool, String> {
    let Some(expected) = expected_w.filter(|&w| w > 0) else {
        return Ok(false);
    };
    if backend.query_power_limits().power_limit_w == Some(expected) {
        return Ok(false);
    }
    let applied =
        super::apply::apply_power_limit(backend, "post-resume re-verification", Some(expected), log)?;
    Ok(applied.is_some())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::gpu::mock::MockGpu;
    use crate::gpu::{GpuError, PowerLimits, NVML_ERROR_NOT_SUPPORTED};

    fn detector() -> SleepGapDetector {
        SleepGapDetector::new(SLEEP_GAP_THRESHOLD_S, 100.0, 500.0)
    }

    #[test]
    fn no_gap_while_awake() {
        let mut det = detector();
        // Both clocks advance in lockstep (including a wedged 60s tick).
        assert_eq!(det.observe(102.0, 502.0), None);
        assert_eq!(det.observe(162.0, 562.0), None);
    }

    #[test]
    fn reports_a_suspend_once() {
        let mut det = detector();
        // 2s of monotonic progress, 3602s of boottime progress: slept ~1h.
        let gap = det.observe(102.0, 4102.0).expect("gap detected");
        assert!((gap - 3600.0).abs() < 1.0, "gap={gap}");
        // Baselines advanced: the same sleep is not re-reported.
        assert_eq!(det.observe(104.0, 4104.0), None);
    }

    #[test]
    fn sub_threshold_jitter_is_ignored() {
        let mut det = detector();
        assert_eq!(det.observe(102.0, 506.0), None); // 4s gap < 5s threshold
    }

    fn mock_with_limits(limit_w: i64, enforced_w: i64) -> MockGpu {
        let mut mock = MockGpu::new();
        mock.power_limits = PowerLimits {
            power_management_enabled: Some(true),
            power_limit_w: Some(limit_w),
            enforced_power_limit_w: Some(enforced_w),
            power_limit_default_w: Some(300),
            power_limit_min_w: Some(150),
            power_limit_max_w: Some(450),
        };
        mock
    }

    fn no_log() -> impl FnMut(&str) {
        |_: &str| {}
    }

    #[test]
    fn power_limit_untouched_when_still_applied() {
        let mock = mock_with_limits(250, 250);
        assert_eq!(verify_power_limit(&mock, Some(250), &mut no_log()), Ok(false));
        assert!(mock.recorded().is_empty(), "no writes expected");
    }

    #[test]
    fn power_limit_reapplied_when_drifted() {
        let mock = mock_with_limits(320, 320);
        // MockGpu's apply_power_limit_w updates its own PowerLimits readback.
        assert_eq!(verify_power_limit(&mock, Some(250), &mut no_log()), Ok(true));
        assert!(!mock.recorded().is_empty(), "a reapply write was expected");
    }

    #[test]
    fn coincidental_enforced_limit_does_not_mask_a_reset() {
        // Management limit reset to 320; a transient cap makes the enforced
        // limit read exactly the profile value. The reset must still be
        // detected and reapplied.
        let mock = mock_with_limits(320, 250);
        assert_eq!(verify_power_limit(&mock, Some(250), &mut no_log()), Ok(true));
        assert!(!mock.recorded().is_empty(), "a reapply write was expected");
    }

    #[test]
    fn no_expected_limit_means_no_reads_or_writes() {
        let mock = mock_with_limits(320, 320);
        assert_eq!(verify_power_limit(&mock, None, &mut no_log()), Ok(false));
        assert_eq!(verify_power_limit(&mock, Some(0), &mut no_log()), Ok(false));
        assert!(mock.recorded().is_empty());
    }

    #[test]
    fn not_supported_reapply_is_tolerated_as_skip() {
        let mut mock = mock_with_limits(320, 320);
        mock.inject_failure(
            "apply_power_limit_w",
            GpuError::nvml("nvmlDeviceSetPowerManagementLimit", NVML_ERROR_NOT_SUPPORTED),
        );
        assert_eq!(verify_power_limit(&mock, Some(250), &mut no_log()), Ok(false));
    }

    #[test]
    fn reapply_failure_is_reported() {
        let mut mock = mock_with_limits(320, 320);
        mock.inject_failure(
            "apply_power_limit_w",
            GpuError::other("injected power failure", 0),
        );
        let err = verify_power_limit(&mock, Some(250), &mut no_log()).expect_err("must fail");
        assert!(err.contains("power limit"), "{err}");
    }
}
