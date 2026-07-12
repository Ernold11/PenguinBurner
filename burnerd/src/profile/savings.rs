//! Energy-saved accounting (issue #23).
//!
//! While a runtime profile is applied and the GPU is under a real workload
//! (a game or another heavy load), every engine tick credits the wall-clock
//! seconds and the watts the active profile saves versus the same scan's
//! stock baseline (`base_avg_power_w - avg_power_w`, both measured under the
//! full Q2RTX load). Totals accumulate across profiles, reboots, and daemon
//! restarts in one root-owned state file, and the supervisor surfaces them in
//! `status` for the UI.

use std::collections::BTreeMap;
use std::env;
use std::io::Write;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::logging;

use super::runtime_spec::{RuntimeProfile, RuntimeSpec};

pub const SAVINGS_STATE_PATH: &str = "/var/lib/penguin-burner/energy-savings.json";
pub const SAVINGS_STATE_FILE_ENV: &str = "PENGUIN_BURNERD_SAVINGS_STATE_FILE";
const SAVINGS_FORMAT_VERSION: u32 = 1;

/// GPU busy percentage that reads as "a workload is running".
const WORKLOAD_UTIL_MIN_PCT: u32 = 70;
/// Fallback when the driver reports no utilization: clocks at or above this
/// fraction of the profile's lock clock mean the GPU left idle territory.
const WORKLOAD_CLOCK_LOCK_FRACTION: f64 = 0.6;
/// One tick can never credit more than this (suspend/resume, wedged NVML
/// calls, and clock jumps must not fabricate hours of savings).
const MAX_TICK_CREDIT_S: f64 = 30.0;
const FLUSH_INTERVAL_S: f64 = 30.0;

pub fn savings_state_path() -> PathBuf {
    if let Some(path) = env::var_os(SAVINGS_STATE_FILE_ENV) {
        if !path.is_empty() {
            return PathBuf::from(path);
        }
    }
    PathBuf::from(SAVINGS_STATE_PATH)
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct SavingsTotals {
    pub format_version: u32,
    /// Seconds a PenguinBurner profile spent under an active GPU workload.
    pub active_seconds: f64,
    /// Accumulated energy saved versus stock, in watt-seconds (joules).
    pub saved_watt_seconds: f64,
}

/// Best-effort load; a missing or unreadable file is a fresh counter, never an
/// error (the daemon must not fail to start over a stats file).
pub fn load_totals(path: &Path) -> SavingsTotals {
    let Ok(raw) = std::fs::read(path) else {
        return SavingsTotals::default();
    };
    serde_json::from_slice(&raw).unwrap_or_default()
}

#[derive(Debug, Clone, Copy)]
struct SavingsRate {
    saved_w: f64,
    high_clock_min_mhz: f64,
}

fn profile_rate(profile: &RuntimeProfile) -> Option<SavingsRate> {
    let avg = profile.avg_power_w?;
    let base = profile.base_avg_power_w?;
    if !(avg.is_finite() && base.is_finite()) || avg <= 0.0 || base <= 0.0 {
        return None;
    }
    Some(SavingsRate {
        // A profile that draws MORE than stock (Performance with a big OC)
        // still counts runtime, just no negative "savings".
        saved_w: (base - avg).max(0.0),
        high_clock_min_mhz: (profile.lock_clock_mhz as f64 * WORKLOAD_CLOCK_LOCK_FRACTION)
            .max(500.0),
    })
}

/// Per-engine accumulator. Constructed from the applied spec; ticked from the
/// engine loop; flushes throttled and once more on drop.
pub struct SavingsTracker {
    path: PathBuf,
    totals: SavingsTotals,
    rates: BTreeMap<String, SavingsRate>,
    last_tick: Option<f64>,
    last_flush: Option<f64>,
    dirty: bool,
    write_failed_logged: bool,
}

impl SavingsTracker {
    /// None when no applied profile carries scan power metrics (old profiles,
    /// stock mode) — there is nothing to account then.
    pub fn from_spec(spec: &RuntimeSpec, path: PathBuf) -> Option<Self> {
        let mut rates = BTreeMap::new();
        if let Some(profile) = &spec.static_profile {
            if let Some(rate) = profile_rate(profile) {
                rates.insert(String::new(), rate);
            }
        }
        if let Some(adaptive) = &spec.adaptive {
            for (tier_key, profile) in &adaptive.profiles {
                if let Some(rate) = profile_rate(profile) {
                    rates.insert(tier_key.clone(), rate);
                }
            }
        }
        if rates.is_empty() {
            return None;
        }
        let totals = load_totals(&path);
        Some(Self {
            path,
            totals,
            rates,
            last_tick: None,
            last_flush: None,
            dirty: false,
            write_failed_logged: false,
        })
    }

    /// One engine tick: `tier_key` is the adaptive tier currently applied
    /// ("" or an unknown key falls back to the static/only rate when
    /// unambiguous). Monotonic `now` in seconds.
    pub fn record(
        &mut self,
        now: f64,
        util_pct: Option<u32>,
        clock_mhz: Option<f64>,
        tier_key: &str,
    ) {
        let dt = match self.last_tick {
            Some(last) => (now - last).clamp(0.0, MAX_TICK_CREDIT_S),
            None => 0.0,
        };
        self.last_tick = Some(now);
        if dt <= 0.0 {
            self.maybe_flush(now);
            return;
        }
        let Some(rate) = self.rate_for(tier_key) else {
            self.maybe_flush(now);
            return;
        };
        let workload_active = match util_pct {
            Some(util) => util >= WORKLOAD_UTIL_MIN_PCT,
            None => clock_mhz.is_some_and(|clock| clock >= rate.high_clock_min_mhz),
        };
        if workload_active {
            self.totals.format_version = SAVINGS_FORMAT_VERSION;
            self.totals.active_seconds += dt;
            self.totals.saved_watt_seconds += rate.saved_w * dt;
            self.dirty = true;
        }
        self.maybe_flush(now);
    }

    fn rate_for(&self, tier_key: &str) -> Option<SavingsRate> {
        if let Some(rate) = self.rates.get(tier_key) {
            return Some(*rate);
        }
        if self.rates.len() == 1 {
            return self.rates.values().next().copied();
        }
        None
    }

    fn maybe_flush(&mut self, now: f64) {
        let due = self
            .last_flush
            .is_none_or(|last| now - last >= FLUSH_INTERVAL_S);
        if self.dirty && due {
            self.flush();
            self.last_flush = Some(now);
        }
    }

    pub fn flush(&mut self) {
        if !self.dirty {
            return;
        }
        match write_totals(&self.path, &self.totals) {
            Ok(()) => {
                self.dirty = false;
            }
            Err(error) => {
                if !self.write_failed_logged {
                    logging::error(&format!(
                        "energy savings state write failed ({}): {error}",
                        self.path.display()
                    ));
                    self.write_failed_logged = true;
                }
            }
        }
    }

    #[cfg(test)]
    pub fn totals(&self) -> &SavingsTotals {
        &self.totals
    }
}

impl Drop for SavingsTracker {
    fn drop(&mut self) {
        self.flush();
    }
}

/// Atomic replace, same pattern as the boot-spec persistence: a status reader
/// must never observe a torn file.
fn write_totals(path: &Path, totals: &SavingsTotals) -> Result<(), String> {
    let parent = path
        .parent()
        .ok_or_else(|| "savings state path has no parent".to_string())?;
    std::fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    let mut temp = tempfile::NamedTempFile::new_in(parent).map_err(|error| error.to_string())?;
    let payload = serde_json::to_vec(totals).map_err(|error| error.to_string())?;
    temp.write_all(&payload).map_err(|error| error.to_string())?;
    temp.persist(path).map_err(|error| error.to_string())?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::profile::runtime_spec::RuntimeSpec;

    fn spec_with_power(avg: Option<f64>, base: Option<f64>) -> RuntimeSpec {
        let mut spec = RuntimeSpec::test_static("GPU-test", "profile-1");
        let profile = spec.static_profile.as_mut().unwrap();
        profile.avg_power_w = avg;
        profile.base_avg_power_w = base;
        profile.lock_clock_mhz = 2700;
        spec
    }

    #[test]
    fn tracker_is_none_without_power_metrics() {
        let spec = spec_with_power(None, None);
        assert!(SavingsTracker::from_spec(&spec, PathBuf::from("/nonexistent")).is_none());
    }

    #[test]
    fn credits_only_loaded_ticks_and_persists_across_instances() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("energy-savings.json");
        let spec = spec_with_power(Some(300.0), Some(340.0));

        {
            let mut tracker = SavingsTracker::from_spec(&spec, path.clone()).unwrap();
            tracker.record(0.0, Some(90), None, "balanced"); // first tick: no dt
            tracker.record(2.0, Some(90), None, "balanced"); // +2s under load
            tracker.record(4.0, Some(10), None, "balanced"); // idle: runtime only ticks over
            tracker.record(6.0, Some(90), None, "balanced"); // +2s under load
            let totals = tracker.totals();
            assert_eq!(totals.active_seconds, 4.0);
            assert_eq!(totals.saved_watt_seconds, 160.0); // 40W * 4s
        } // drop flushes

        let reloaded = load_totals(&path);
        assert_eq!(reloaded.active_seconds, 4.0);
        assert_eq!(reloaded.saved_watt_seconds, 160.0);

        // A fresh engine keeps accumulating on top of the persisted totals.
        let mut tracker = SavingsTracker::from_spec(&spec, path.clone()).unwrap();
        tracker.record(100.0, Some(90), None, "balanced");
        tracker.record(101.0, Some(90), None, "balanced");
        tracker.flush();
        assert_eq!(load_totals(&path).active_seconds, 5.0);
    }

    #[test]
    fn clock_fallback_detects_load_when_utilization_is_unavailable() {
        let dir = tempfile::tempdir().unwrap();
        let spec = spec_with_power(Some(300.0), Some(340.0));
        let mut tracker =
            SavingsTracker::from_spec(&spec, dir.path().join("s.json")).unwrap();
        // 60% of the 2700MHz lock = 1620MHz threshold.
        tracker.record(0.0, None, Some(2400.0), "balanced");
        tracker.record(1.0, None, Some(2400.0), "balanced"); // loaded
        tracker.record(2.0, None, Some(400.0), "balanced"); // idle clocks
        assert_eq!(tracker.totals().active_seconds, 1.0);
    }

    #[test]
    fn suspend_gap_cannot_fabricate_hours() {
        let dir = tempfile::tempdir().unwrap();
        let spec = spec_with_power(Some(300.0), Some(340.0));
        let mut tracker =
            SavingsTracker::from_spec(&spec, dir.path().join("s.json")).unwrap();
        tracker.record(0.0, Some(90), None, "balanced");
        tracker.record(7200.0, Some(90), None, "balanced"); // 2h gap → 30s max
        assert_eq!(tracker.totals().active_seconds, MAX_TICK_CREDIT_S);
    }

    #[test]
    fn unknown_tier_falls_back_only_when_unambiguous() {
        let dir = tempfile::tempdir().unwrap();
        let spec = spec_with_power(Some(300.0), Some(340.0));
        let mut tracker =
            SavingsTracker::from_spec(&spec, dir.path().join("s.json")).unwrap();
        // Static profiles carry a tier key that is not in the single-entry
        // rate map; the sole rate still applies.
        tracker.record(0.0, Some(90), None, "whatever");
        tracker.record(1.0, Some(90), None, "whatever");
        assert_eq!(tracker.totals().saved_watt_seconds, 40.0);
    }

    #[test]
    fn negative_savings_count_runtime_but_no_energy() {
        let dir = tempfile::tempdir().unwrap();
        // Performance profile drawing MORE than stock.
        let spec = spec_with_power(Some(360.0), Some(340.0));
        let mut tracker =
            SavingsTracker::from_spec(&spec, dir.path().join("s.json")).unwrap();
        tracker.record(0.0, Some(90), None, "performance");
        tracker.record(1.0, Some(90), None, "performance");
        assert_eq!(tracker.totals().active_seconds, 1.0);
        assert_eq!(tracker.totals().saved_watt_seconds, 0.0);
    }
}
