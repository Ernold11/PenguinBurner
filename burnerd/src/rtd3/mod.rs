//! Deep-sleep (RTD3/D3cold) policy for hybrid laptops.
//!
//! On an RTD3-capable machine the daemon must not hold NVML or NVAPI handles
//! while the dGPU is idle: any open `/dev/nvidia*` client pins the GPU in D0
//! and defeats runtime suspend (issue #30). This module owns that policy:
//!
//! - `detect`: wake-free probes of the PCI sysfs tree and the NVIDIA procfs
//!   `power` file, and the startup verdict (`DeepSleepDecision`).
//! - `watch`: the startup supervisor that defers the persisted runtime while
//!   the GPU sleeps, starts it when a real client wakes the GPU, and sweeps
//!   idle RPC backends so a one-off telemetry query cannot pin the GPU.
//! - the process-wide gate (this file): the cached verdict every attach point
//!   consults, plus the `deep_sleep` block reported by `status`.
//!
//! Desktops resolve to `Disabled` and keep the classic always-attached
//! behavior byte-for-byte.

pub mod detect;
pub mod watch;

use std::sync::Mutex;

use serde::Serialize;

use crate::logging;
use detect::{DeepSleepDecision, RuntimePmStatus};

/// The `deep_sleep` object in the `status` response. Field order is the wire
/// contract (api.rs serializes structs in declaration order).
#[derive(Debug, Clone, Serialize)]
pub struct DeepSleepStatus {
    /// "armed" | "disabled" | "unknown"
    pub state: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    /// "fine-grained" | "coarse-grained" when armed.
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
    decision: Option<DeepSleepDecision>,
    pci_addr: Option<String>,
    last_runtime_status: Option<RuntimePmStatus>,
    suspended_observed: bool,
    autostart_deferred: bool,
}

static GATE: Mutex<GateState> = Mutex::new(GateState {
    decision: None,
    pci_addr: None,
    last_runtime_status: None,
    suspended_observed: false,
    autostart_deferred: false,
});

fn gate() -> std::sync::MutexGuard<'static, GateState> {
    GATE.lock().unwrap_or_else(|poison| poison.into_inner())
}

/// Resolve the GPU address (persisted-spec hint first, then PCI enumeration)
/// and store the startup verdict. Wake-free; call before any NVML init.
pub fn evaluate(probe: &detect::Rtd3Probe, pci_hint: Option<&str>) -> DeepSleepDecision {
    let addr = pci_hint
        .and_then(detect::sysfs_pci_addr)
        .filter(|addr| probe.is_nvidia_gpu(addr))
        .or_else(|| probe.first_nvidia_gpu_addr());
    let decision = match &addr {
        Some(addr) => detect::assess(probe, addr),
        None => DeepSleepDecision::Disabled {
            reason: "no NVIDIA display-class PCI device visible",
        },
    };
    let mut state = gate();
    state.pci_addr = addr;
    if state.decision.as_ref() != Some(&decision) {
        logging::info(&format!(
            "deep sleep gate: {} (gpu={})",
            describe(&decision),
            state.pci_addr.as_deref().unwrap_or("unknown"),
        ));
    }
    state.decision = Some(decision.clone());
    decision
}

fn describe(decision: &DeepSleepDecision) -> String {
    match decision {
        DeepSleepDecision::Armed { fine_grained } => format!(
            "armed ({} RTD3)",
            if *fine_grained {
                "fine-grained"
            } else {
                "coarse-grained"
            }
        ),
        DeepSleepDecision::Disabled { reason } => format!("disabled: {reason}"),
        DeepSleepDecision::Unknown { reason } => format!("undecided: {reason}"),
    }
}

/// True when NVML/NVAPI handles must be treated as ephemeral. Sticky: a GPU
/// observed suspended arms the gate even if the config files were unreadable.
pub fn is_armed() -> bool {
    let state = gate();
    matches!(state.decision, Some(DeepSleepDecision::Armed { .. })) || state.suspended_observed
}

pub fn current_decision() -> Option<DeepSleepDecision> {
    gate().decision.clone()
}

pub fn pci_addr() -> Option<String> {
    gate().pci_addr.clone()
}

/// Record a watcher observation. First `suspended` sighting arms the gate for
/// the rest of the daemon's life (hardware proof beats config parsing).
pub fn note_runtime_status(status: RuntimePmStatus) {
    let mut state = gate();
    state.last_runtime_status = Some(status);
    if status == RuntimePmStatus::Suspended && !state.suspended_observed {
        state.suspended_observed = true;
        if !matches!(state.decision, Some(DeepSleepDecision::Armed { .. })) {
            logging::info("deep sleep gate: GPU observed suspended; arming deep-sleep handling");
        }
    }
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
    let decision = state.decision.as_ref()?;
    let armed =
        matches!(decision, DeepSleepDecision::Armed { .. }) || state.suspended_observed;
    let (reason, mode) = match decision {
        DeepSleepDecision::Armed { fine_grained } => (
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
        DeepSleepDecision::Disabled { reason } | DeepSleepDecision::Unknown { reason } => {
            (Some((*reason).to_string()), None)
        }
    };
    Some(DeepSleepStatus {
        state: if armed {
            "armed".to_string()
        } else if matches!(decision, DeepSleepDecision::Disabled { .. }) {
            "disabled".to_string()
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

    /// The gate is process-global; serialize the tests that mutate it.
    static TEST_LOCK: Mutex<()> = Mutex::new(());

    #[test]
    fn sticky_suspended_observation_arms_the_gate() {
        let _guard = TEST_LOCK.lock().unwrap_or_else(|poison| poison.into_inner());
        reset_for_test();
        assert!(!is_armed());
        note_runtime_status(RuntimePmStatus::Active);
        assert!(!is_armed());
        note_runtime_status(RuntimePmStatus::Suspended);
        assert!(is_armed());
        note_runtime_status(RuntimePmStatus::Active);
        assert!(is_armed(), "suspended observation must be sticky");
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
