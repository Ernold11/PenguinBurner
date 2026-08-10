//! Deep-sleep (RTD3/D3cold) policy for hybrid laptops.
//!
//! On an RTD3-capable machine the daemon must not hold NVML or NVAPI handles
//! while the dGPU is idle: any open `/dev/nvidia*` client pins the GPU in D0
//! and defeats runtime suspend (issue #30). This module owns that policy:
//!
//! - `detect`: wake-free probes of the PCI sysfs tree and the NVIDIA procfs
//!   `power` file, and the startup mode (`DeepSleepMode`).
//! - `watch`: the startup supervisor that defers the persisted runtime while
//!   the GPU sleeps, starts it when a real client wakes the GPU, and sweeps
//!   idle RPC backends so a one-off telemetry query cannot pin the GPU.
//! - the process-wide gate (this file): the cached mode every attach point
//!   consults, plus the `deep_sleep` block reported by `status`.
//!
//! Desktop mode keeps the existing always-attached behavior. Mobile mode keeps
//! GPU handles ephemeral so an RTD3-capable dGPU can suspend.

pub mod detect;
pub mod watch;

use std::sync::Mutex;

use serde::Serialize;

use crate::logging;
use detect::{DeepSleepMode, RuntimePmStatus};

/// The `deep_sleep` object in the `status` response. Field order is the wire
/// contract (api.rs serializes structs in declaration order).
#[derive(Debug, Clone, Serialize)]
pub struct DeepSleepStatus {
    /// "mobile" | "desktop" | "unknown"
    pub state: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    /// "fine-grained" | "coarse-grained" in mobile mode.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub mode: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pci_addr: Option<String>,
    /// Last observed kernel runtime-PM state ("suspended", "active", ...).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub runtime_status: Option<String>,
    /// True while a persisted runtime exists but its start is held back
    /// because the GPU is asleep.
    pub autostart_deferred: bool,
    /// True once the GPU has been seen suspended this daemon lifetime —
    /// definitive evidence the platform can deep-sleep.
    pub suspended_observed: bool,
}

#[derive(Default)]
struct GateState {
    mode: Option<DeepSleepMode>,
    pci_addr: Option<String>,
    last_runtime_status: Option<RuntimePmStatus>,
    suspended_observed: bool,
    autostart_deferred: bool,
}

static GATE: Mutex<GateState> = Mutex::new(GateState {
    mode: None,
    pci_addr: None,
    last_runtime_status: None,
    suspended_observed: false,
    autostart_deferred: false,
});

fn gate() -> std::sync::MutexGuard<'static, GateState> {
    GATE.lock().unwrap_or_else(|poison| poison.into_inner())
}

/// Resolve the GPU address (persisted-spec hint first, then PCI enumeration),
/// record its cached runtime status, and store the startup mode. Wake-free;
/// call before any NVML init.
pub fn evaluate(probe: &detect::Rtd3Probe, pci_hint: Option<&str>) -> DeepSleepMode {
    let normalized_hint = pci_hint.and_then(detect::sysfs_pci_addr);
    let addr = match normalized_hint {
        Some(addr) if probe.is_nvidia_gpu(&addr) => Some(addr),
        // A saved device may not be bound yet, or its PCI address may have
        // changed. Prefer any visible NVIDIA GPU; preserve an absent address
        // only when enumeration has nothing better so boot stays Unknown.
        Some(addr) if !probe.device_present(&addr) => {
            probe.first_nvidia_gpu_addr().or(Some(addr))
        }
        // A visible hint that no longer identifies an NVIDIA display device is
        // stale; enumerate rather than trusting the saved address.
        _ => probe.first_nvidia_gpu_addr(),
    };
    let mode = match &addr {
        Some(addr) => detect::assess(probe, addr),
        None => DeepSleepMode::Unknown {
            reason: "no NVIDIA display-class PCI device visible",
        },
    };
    let runtime_status = addr.as_deref().map(|addr| probe.runtime_pm_status(addr));
    let mut state = gate();
    state.pci_addr = addr;
    if let Some(status) = runtime_status {
        record_runtime_status(&mut state, status);
    }
    if state.mode.as_ref() != Some(&mode) {
        logging::info(&format!(
            "deep sleep mode: {} (gpu={})",
            describe(&mode),
            state.pci_addr.as_deref().unwrap_or("unknown"),
        ));
    }
    state.mode = Some(mode.clone());
    mode
}

fn describe(mode: &DeepSleepMode) -> String {
    match mode {
        DeepSleepMode::Mobile { fine_grained } => format!(
            "mobile ({} RTD3)",
            if *fine_grained {
                "fine-grained"
            } else {
                "coarse-grained"
            }
        ),
        DeepSleepMode::Desktop { reason } => format!("desktop: {reason}"),
        DeepSleepMode::Unknown { reason } => format!("unknown: {reason}"),
    }
}

/// True when the effective mode is Mobile. Sticky: observing a suspended GPU
/// is definitive evidence that mobile deep-sleep handling is required even if
/// the configuration files were unreadable or later change.
#[cfg(test)]
pub(crate) fn is_mobile_mode() -> bool {
    let state = gate();
    matches!(state.mode, Some(DeepSleepMode::Mobile { .. })) || state.suspended_observed
}

/// True when eager attachment and long-lived GPU handles are unsafe. Unknown
/// follows conservative Mobile behavior until detection resolves.
pub fn protects_deep_sleep() -> bool {
    let state = gate();
    matches!(
        state.mode,
        Some(DeepSleepMode::Mobile { .. }) | Some(DeepSleepMode::Unknown { .. })
    ) || state.suspended_observed
}

/// Only a definite Desktop mode may use eager startup.
pub fn allows_desktop_autostart() -> bool {
    let state = gate();
    matches!(state.mode, Some(DeepSleepMode::Desktop { .. })) && !state.suspended_observed
}

/// Persistence mode blocks runtime D3, so Mobile and Unknown suppress it.
pub fn suppress_persistence() -> bool {
    protects_deep_sleep()
}

pub fn last_runtime_status() -> Option<RuntimePmStatus> {
    gate().last_runtime_status
}

fn record_runtime_status(state: &mut GateState, status: RuntimePmStatus) {
    state.last_runtime_status = Some(status);
    if status == RuntimePmStatus::Suspended && !state.suspended_observed {
        state.suspended_observed = true;
        if !matches!(state.mode, Some(DeepSleepMode::Mobile { .. })) {
            logging::info("deep sleep mode: GPU observed suspended; selecting Mobile handling");
        }
    }
}

/// Record a watcher observation. First `suspended` sighting selects Mobile
/// handling for the rest of the daemon's life (hardware proof beats parsing).
#[cfg(test)]
pub(crate) fn note_runtime_status(status: RuntimePmStatus) {
    let mut state = gate();
    record_runtime_status(&mut state, status);
}

pub fn set_autostart_deferred(deferred: bool) {
    let mut state = gate();
    if state.autostart_deferred != deferred {
        state.autostart_deferred = deferred;
        logging::info(if deferred {
            "deep sleep: persisted runtime deferred until the GPU is in use"
        } else {
            "deep sleep: runtime deferral cleared"
        });
    }
}

/// The `deep_sleep` status block; `None` until `evaluate` has run (unit tests
/// that build a bare supervisor never see the field).
pub fn status() -> Option<DeepSleepStatus> {
    let state = gate();
    let detected_mode = state.mode.as_ref()?;
    let mobile =
        matches!(detected_mode, DeepSleepMode::Mobile { .. }) || state.suspended_observed;
    let (reason, mode) = match detected_mode {
        DeepSleepMode::Mobile { fine_grained } => (
            None,
            Some(
                if *fine_grained {
                    "fine-grained"
                } else {
                    "coarse-grained"
                }
                .to_string(),
            ),
        ),
        DeepSleepMode::Desktop { reason } | DeepSleepMode::Unknown { reason } => {
            (Some((*reason).to_string()), None)
        }
    };
    Some(DeepSleepStatus {
        state: if mobile {
            "mobile".to_string()
        } else if matches!(detected_mode, DeepSleepMode::Desktop { .. }) {
            "desktop".to_string()
        } else {
            "unknown".to_string()
        },
        reason,
        mode,
        pci_addr: state.pci_addr.clone(),
        runtime_status: state.last_runtime_status.map(|s| s.as_str().to_string()),
        autostart_deferred: state.autostart_deferred,
        suspended_observed: state.suspended_observed,
    })
}

#[cfg(test)]
pub(crate) fn reset_for_test() {
    let mut state = gate();
    *state = GateState::default();
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    /// The gate is process-global; serialize the tests that mutate it.
    static TEST_LOCK: Mutex<()> = Mutex::new(());

    #[test]
    fn sticky_suspended_observation_selects_mobile_mode() {
        let _guard = TEST_LOCK.lock().unwrap_or_else(|poison| poison.into_inner());
        reset_for_test();
        assert!(!is_mobile_mode());
        note_runtime_status(RuntimePmStatus::Active);
        assert!(!is_mobile_mode());
        note_runtime_status(RuntimePmStatus::Suspended);
        assert!(is_mobile_mode());
        note_runtime_status(RuntimePmStatus::Active);
        assert!(is_mobile_mode(), "suspended observation must be sticky");
        reset_for_test();
    }

    #[test]
    fn absent_persisted_hint_stays_unknown_and_is_preserved() {
        let _guard = TEST_LOCK.lock().unwrap_or_else(|poison| poison.into_inner());
        reset_for_test();
        let root = tempfile::tempdir().expect("tempdir");
        let probe = detect::Rtd3Probe::with_roots(
            &root.path().join("sys"),
            &root.path().join("proc"),
        );

        assert!(matches!(
            evaluate(&probe, Some("00000000:01:00.0")),
            DeepSleepMode::Unknown { .. }
        ));
        assert_eq!(
            status().and_then(|status| status.pci_addr).as_deref(),
            Some("0000:01:00.0")
        );
        assert!(protects_deep_sleep());
        assert!(!allows_desktop_autostart());
        reset_for_test();
    }

    #[test]
    fn visible_nvidia_gpu_replaces_an_absent_persisted_hint() {
        let _guard = TEST_LOCK.lock().unwrap_or_else(|poison| poison.into_inner());
        reset_for_test();
        let root = tempfile::tempdir().expect("tempdir");
        let addr = "0000:02:00.0";
        let device = root.path().join("sys").join(addr);
        fs::create_dir_all(device.join("power")).unwrap();
        fs::write(device.join("vendor"), "0x10de\n").unwrap();
        fs::write(device.join("class"), "0x030000\n").unwrap();
        fs::write(device.join("power/control"), "auto\n").unwrap();
        fs::write(device.join("power/runtime_status"), "active\n").unwrap();
        let proc_gpu = root.path().join("proc").join(addr);
        fs::create_dir_all(&proc_gpu).unwrap();
        fs::write(
            proc_gpu.join("power"),
            "Runtime D3 status:          Enabled (fine-grained)\n",
        )
        .unwrap();
        let probe = detect::Rtd3Probe::with_roots(
            &root.path().join("sys"),
            &root.path().join("proc"),
        );

        assert!(matches!(
            evaluate(&probe, Some("00000000:01:00.0")),
            DeepSleepMode::Mobile { .. }
        ));
        assert_eq!(
            status().and_then(|status| status.pci_addr).as_deref(),
            Some(addr)
        );
        reset_for_test();
    }

    #[test]
    fn status_is_absent_until_evaluated() {
        let _guard = TEST_LOCK.lock().unwrap_or_else(|poison| poison.into_inner());
        reset_for_test();
        assert!(status().is_none());
        reset_for_test();
    }
}
