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

mod clients;
pub mod detect;
pub mod watch;

use std::collections::HashSet;
use std::sync::Mutex;
use std::time::{Duration, Instant};

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
    /// Cumulative milliseconds the GPU has spent runtime-active since boot
    /// (kernel counter, wake-free read). With `runtime_suspended_ms` this
    /// quantifies how much the GPU actually sleeps.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub runtime_active_ms: Option<u64>,
    /// Cumulative milliseconds runtime-suspended since boot.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub runtime_suspended_ms: Option<u64>,
    /// True while a persisted runtime exists but its start is held back
    /// because the GPU is asleep.
    pub autostart_deferred: bool,
    /// True once the GPU has been seen suspended this daemon lifetime —
    /// definitive evidence the platform can deep-sleep.
    pub suspended_observed: bool,
    /// True while an applied profile is parked: its engine released the GPU
    /// so it can suspend, and the watcher reapplies it at the next real use.
    pub parked: bool,
    /// The watcher's most recent client sample (mobile modes only): which
    /// processes really use the GPU, per kind, with aggregate counts.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub gpu_clients: Option<GpuClientsStatus>,
}

/// One process in the `gpu_clients` lists.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct GpuClientProcess {
    pub pid: u32,
    /// `/proc/<pid>/comm`; `null` when the process exited before the lookup.
    pub name: Option<String>,
}

/// Where a client sample came from.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GpuClientSource {
    /// Live NVML graphics/compute contexts — the decisive signal under
    /// fine-grained RTD3.
    NvmlContexts,
    /// `/dev/nvidia<N>` device-node fd holders — the decisive signal under
    /// coarse-grained/unknown RTD3, and the fallback when NVML is unavailable.
    DeviceNodes,
}

impl GpuClientSource {
    fn as_str(self) -> &'static str {
        match self {
            Self::NvmlContexts => "nvml-contexts",
            Self::DeviceNodes => "device-nodes",
        }
    }
}

/// A watcher client sample: who holds live GPU contexts and who merely holds
/// device nodes open, minus the daemon itself.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GpuClientObservation {
    pub source: GpuClientSource,
    pub graphics: Vec<GpuClientProcess>,
    pub compute: Vec<GpuClientProcess>,
    pub device_node_holders: Vec<GpuClientProcess>,
    /// Positively identified infrastructure helpers excluded from the verdict
    /// in fine-grained mode, retained so status and logs explain the decision.
    pub ignored_clients: Vec<GpuClientProcess>,
}

impl GpuClientObservation {
    /// The unique PIDs the park/wake decision counts: the context lists under
    /// `NvmlContexts`, the fd holders under `DeviceNodes`. The single
    /// definition of "decisive" — the watcher's verdict, the status block's
    /// `total_count`, and the journal line all derive from it.
    pub(crate) fn counted_pids(&self) -> HashSet<u32> {
        match self.source {
            // Context lists already contain only counted clients. If a
            // contradictory ignored entry ever survives classification,
            // fail closed: an explicitly counted context wins.
            GpuClientSource::NvmlContexts => self
                .graphics
                .iter()
                .chain(&self.compute)
                .map(|process| process.pid)
                .collect(),
            // The fallback retains every observed holder for diagnostics and
            // subtracts only the positively identified helper PIDs.
            GpuClientSource::DeviceNodes => {
                let ignored: HashSet<u32> = self
                    .ignored_clients
                    .iter()
                    .map(|process| process.pid)
                    .collect();
                self.device_node_holders
                    .iter()
                    .filter(|process| !ignored.contains(&process.pid))
                    .map(|process| process.pid)
                    .collect()
            }
        }
    }

    /// True when the decision must treat the GPU as in use.
    pub(crate) fn in_use(&self) -> bool {
        !self.counted_pids().is_empty()
    }
}

/// The `gpu_clients` object inside `deep_sleep`: the watcher's latest client
/// sample, kept so someone debugging why the GPU does or does not park
/// (issue #30) can see exactly what the daemon saw.
#[derive(Debug, Clone, Serialize)]
pub struct GpuClientsStatus {
    /// "nvml-contexts" or "device-nodes" — which signal decided park/wake.
    pub source: String,
    pub graphics_count: usize,
    pub compute_count: usize,
    pub device_node_count: usize,
    pub ignored_count: usize,
    /// Unique PIDs the park/wake decision counted (the context lists under
    /// "nvml-contexts", the fd holders under "device-nodes"). Parking
    /// proceeds only while this stays 0.
    pub total_count: usize,
    /// Seconds since the sample was taken. Grows while the GPU sleeps —
    /// the watcher only samples an awake GPU.
    pub age_s: f64,
    pub graphics: Vec<GpuClientProcess>,
    pub compute: Vec<GpuClientProcess>,
    /// Infrastructure helpers observed but excluded from the fine-grained
    /// client decision.
    pub ignored_clients: Vec<GpuClientProcess>,
    /// Processes holding `/dev/nvidia<N>` open. Under fine-grained RTD3 this
    /// is informational: a holder with no live context does not keep the GPU
    /// awake and does not block parking.
    pub device_node_holders: Vec<GpuClientProcess>,
}

fn gpu_clients_status(observation: &GpuClientObservation, observed_at: Instant) -> GpuClientsStatus {
    GpuClientsStatus {
        source: observation.source.as_str().to_string(),
        graphics_count: observation.graphics.len(),
        compute_count: observation.compute.len(),
        device_node_count: observation.device_node_holders.len(),
        ignored_count: observation.ignored_clients.len(),
        total_count: observation.counted_pids().len(),
        age_s: (observed_at.elapsed().as_secs_f64() * 1000.0).round() / 1000.0,
        graphics: observation.graphics.clone(),
        compute: observation.compute.clone(),
        ignored_clients: observation.ignored_clients.clone(),
        device_node_holders: observation.device_node_holders.clone(),
    }
}

#[derive(Default)]
struct GateState {
    mode: Option<DeepSleepMode>,
    pci_addr: Option<String>,
    last_runtime_status: Option<RuntimePmStatus>,
    /// When `last_runtime_status` last changed, so each transition line can
    /// say how long the previous state was held.
    runtime_status_since: Option<Instant>,
    runtime_active_ms: Option<u64>,
    runtime_suspended_ms: Option<u64>,
    autosuspend_delay: Option<Duration>,
    suspended_observed: bool,
    autostart_deferred: bool,
    parked: bool,
    gpu_clients: Option<(GpuClientObservation, Instant)>,
}

static GATE: Mutex<GateState> = Mutex::new(GateState {
    mode: None,
    pci_addr: None,
    last_runtime_status: None,
    runtime_status_since: None,
    runtime_active_ms: None,
    runtime_suspended_ms: None,
    autosuspend_delay: None,
    suspended_observed: false,
    autostart_deferred: false,
    parked: false,
    gpu_clients: None,
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
    let runtime_times = addr.as_deref().map(|addr| probe.runtime_pm_times(addr));
    let autosuspend_delay = addr
        .as_deref()
        .and_then(|addr| probe.autosuspend_delay(addr));
    let mut state = gate();
    state.pci_addr = addr;
    if let Some(status) = runtime_status {
        record_runtime_status(&mut state, status);
    }
    if let Some((active_ms, suspended_ms)) = runtime_times {
        state.runtime_active_ms = active_ms;
        state.runtime_suspended_ms = suspended_ms;
    }
    state.autosuspend_delay = autosuspend_delay;
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

/// True when the driver reports fine-grained runtime D3: it holds the GPU
/// awake per submitted work, not per open fd, so a `/dev/nvidia<N>` holder is
/// not evidence of use and the watcher must ask NVML for live contexts
/// instead. Deliberately NOT satisfied by the sticky suspended-observed
/// fallback — only the parsed driver report proves fine-grained semantics.
pub fn fine_grained_mode() -> bool {
    matches!(
        gate().mode,
        Some(DeepSleepMode::Mobile { fine_grained: true })
    )
}

pub fn last_runtime_status() -> Option<RuntimePmStatus> {
    gate().last_runtime_status
}

pub(crate) fn autosuspend_delay() -> Option<Duration> {
    gate().autosuspend_delay
}

fn record_runtime_status(state: &mut GateState, status: RuntimePmStatus) {
    // Transition line: the kernel-side timeline of the GPU's sleep. Emitted
    // only when the state changes (the watcher re-reads at 1 Hz), with how
    // long the previous state was held, so the journal shows exactly when
    // the GPU suspended, when it woke, and how long each episode lasted.
    match state.last_runtime_status {
        Some(previous) if previous == status => {}
        Some(previous) => {
            let held = state
                .runtime_status_since
                .map(|since| format!(" ({} for {:.1}s)", previous.as_str(), since.elapsed().as_secs_f64()))
                .unwrap_or_default();
            logging::info(&format!(
                "deep sleep: runtime_status {} -> {}{held}",
                previous.as_str(),
                status.as_str(),
            ));
            state.runtime_status_since = Some(Instant::now());
        }
        None => {
            logging::info(&format!(
                "deep sleep: runtime_status {} (initial)",
                status.as_str()
            ));
            state.runtime_status_since = Some(Instant::now());
        }
    }
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

/// Record that the applied runtime was parked (engine stopped, persisted
/// state kept) so the GPU can suspend — or that it re-materialized.
pub fn set_parked(parked: bool) {
    let mut state = gate();
    if state.parked != parked {
        state.parked = parked;
        logging::info(if parked {
            "deep sleep: runtime parked; the GPU may suspend until its next real use"
        } else {
            "deep sleep: parked runtime re-materialized"
        });
    }
}

/// Record the watcher's latest client sample for the `gpu_clients` status
/// block. Called on every awake-GPU check, so the block always reflects the
/// evidence behind the most recent park/wake decision.
///
/// A journal line is emitted only when the sample differs from the previous
/// one: the watcher samples at 1 Hz, and the client set changes on the order
/// of process starts and exits, so the journal reads as a sparse narrative
/// of who took and released the GPU instead of a per-second flood.
pub(crate) fn record_gpu_clients(observation: GpuClientObservation) {
    let mut state = gate();
    let changed = state
        .gpu_clients
        .as_ref()
        .is_none_or(|(previous, _)| previous != &observation);
    if changed {
        logging::info(&describe_gpu_clients(&observation));
    }
    state.gpu_clients = Some((observation, Instant::now()));
}

/// One journal line carrying the decision verdict plus every sampled process
/// with its count per list, e.g. `deep sleep: gpu clients (nvml-contexts):
/// counted=1 (GPU in use), graphics=1 [4242 some-game], compute=0,
/// device-node holders=2 [1450 plasmashell, 3117 vkcube]`.
fn describe_gpu_clients(observation: &GpuClientObservation) -> String {
    fn list(label: &str, processes: &[GpuClientProcess]) -> String {
        if processes.is_empty() {
            return format!("{label}=0");
        }
        let entries: Vec<String> = processes
            .iter()
            .map(|process| match &process.name {
                Some(name) => format!("{} {name}", process.pid),
                None => process.pid.to_string(),
            })
            .collect();
        format!("{label}={} [{}]", processes.len(), entries.join(", "))
    }
    format!(
        "deep sleep: gpu clients ({}): counted={} ({}), {}, {}, {}, {}",
        observation.source.as_str(),
        observation.counted_pids().len(),
        if observation.in_use() {
            "GPU in use"
        } else {
            "idle"
        },
        list("graphics", &observation.graphics),
        list("compute", &observation.compute),
        list(
            "ignored infrastructure helpers",
            &observation.ignored_clients,
        ),
        list("device-node holders", &observation.device_node_holders),
    )
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
        runtime_active_ms: state.runtime_active_ms,
        runtime_suspended_ms: state.runtime_suspended_ms,
        autostart_deferred: state.autostart_deferred,
        suspended_observed: state.suspended_observed,
        parked: state.parked,
        gpu_clients: state
            .gpu_clients
            .as_ref()
            .map(|(observation, observed_at)| gpu_clients_status(observation, *observed_at)),
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
    fn fine_grained_mode_follows_the_parsed_driver_report_only() {
        let _guard = TEST_LOCK.lock().unwrap_or_else(|poison| poison.into_inner());
        reset_for_test();
        let root = tempfile::tempdir().expect("tempdir");
        let addr = "0000:01:00.0";
        let device = root.path().join("sys").join(addr);
        fs::create_dir_all(device.join("power")).unwrap();
        fs::write(device.join("vendor"), "0x10de\n").unwrap();
        fs::write(device.join("class"), "0x030000\n").unwrap();
        fs::write(device.join("power/control"), "auto\n").unwrap();
        fs::write(device.join("power/runtime_status"), "active\n").unwrap();
        let proc_gpu = root.path().join("proc").join(addr);
        fs::create_dir_all(&proc_gpu).unwrap();
        let probe = detect::Rtd3Probe::with_roots(
            &root.path().join("sys"),
            &root.path().join("proc"),
        );

        fs::write(
            proc_gpu.join("power"),
            "Runtime D3 status:          Enabled (fine-grained)\n",
        )
        .unwrap();
        evaluate(&probe, None);
        assert!(fine_grained_mode());

        fs::write(
            proc_gpu.join("power"),
            "Runtime D3 status:          Enabled (coarse-grained)\n",
        )
        .unwrap();
        evaluate(&probe, None);
        assert!(!fine_grained_mode());
        // The sticky suspended observation selects Mobile handling but must
        // NOT claim fine-grained fd semantics.
        note_runtime_status(RuntimePmStatus::Suspended);
        assert!(is_mobile_mode());
        assert!(!fine_grained_mode());
        reset_for_test();
    }

    #[test]
    fn status_is_absent_until_evaluated() {
        let _guard = TEST_LOCK.lock().unwrap_or_else(|poison| poison.into_inner());
        reset_for_test();
        assert!(status().is_none());
        reset_for_test();
    }

    fn client(pid: u32, name: &str) -> GpuClientProcess {
        GpuClientProcess {
            pid,
            name: Some(name.to_string()),
        }
    }

    #[test]
    fn gpu_client_stats_count_only_the_decisive_lists() {
        let observation = GpuClientObservation {
            source: GpuClientSource::NvmlContexts,
            graphics: vec![client(100, "gnome-shell"), client(200, "game")],
            compute: vec![client(200, "game"), client(300, "python3")],
            device_node_holders: vec![client(100, "gnome-shell"), client(999, "Xorg")],
            ignored_clients: Vec::new(),
        };
        let stats = gpu_clients_status(&observation, Instant::now());
        assert_eq!(stats.source, "nvml-contexts");
        assert_eq!(stats.graphics_count, 2);
        assert_eq!(stats.compute_count, 2);
        assert_eq!(stats.device_node_count, 2);
        // A PID with both context kinds counts once; under fine-grained RTD3
        // fd holders are informational and never counted.
        assert_eq!(stats.total_count, 3);

        let observation = GpuClientObservation {
            source: GpuClientSource::DeviceNodes,
            graphics: Vec::new(),
            compute: Vec::new(),
            device_node_holders: vec![client(999, "Xorg")],
            ignored_clients: Vec::new(),
        };
        let stats = gpu_clients_status(&observation, Instant::now());
        assert_eq!(stats.source, "device-nodes");
        assert_eq!(stats.total_count, 1);
        assert_eq!(stats.graphics_count, 0);
        assert_eq!(stats.compute_count, 0);
    }

    #[test]
    fn counted_context_wins_over_a_contradictory_ignored_entry() {
        let observation = GpuClientObservation {
            source: GpuClientSource::NvmlContexts,
            graphics: vec![client(880, "nvidia-powerd")],
            compute: Vec::new(),
            device_node_holders: Vec::new(),
            ignored_clients: vec![client(880, "nvidia-powerd")],
        };
        assert!(observation.in_use());
        assert_eq!(observation.counted_pids(), HashSet::from([880]));
    }

    #[test]
    fn gpu_client_log_line_names_every_process_with_counts() {
        let observation = GpuClientObservation {
            source: GpuClientSource::NvmlContexts,
            graphics: vec![client(4242, "some-game")],
            compute: Vec::new(),
            device_node_holders: vec![
                client(1450, "plasmashell"),
                GpuClientProcess {
                    pid: 3117,
                    name: None,
                },
            ],
            ignored_clients: Vec::new(),
        };
        assert_eq!(
            describe_gpu_clients(&observation),
            "deep sleep: gpu clients (nvml-contexts): counted=1 (GPU in use), \
             graphics=1 [4242 some-game], compute=0, \
             ignored infrastructure helpers=0, \
             device-node holders=2 [1450 plasmashell, 3117]"
        );

        // Fine-grained with only fd holders: counted=0 reads as idle even
        // though the holder list is not empty — the line itself explains a
        // park that happens under a running desktop shell.
        let observation = GpuClientObservation {
            source: GpuClientSource::NvmlContexts,
            graphics: Vec::new(),
            compute: Vec::new(),
            device_node_holders: vec![client(1450, "plasmashell")],
            ignored_clients: Vec::new(),
        };
        assert!(!observation.in_use());
        assert_eq!(
            describe_gpu_clients(&observation),
            "deep sleep: gpu clients (nvml-contexts): counted=0 (idle), \
             graphics=0, compute=0, ignored infrastructure helpers=0, \
             device-node holders=1 [1450 plasmashell]"
        );
    }

    #[test]
    fn status_reports_the_recorded_client_sample() {
        let _guard = TEST_LOCK.lock().unwrap_or_else(|poison| poison.into_inner());
        reset_for_test();
        let root = tempfile::tempdir().expect("tempdir");
        let probe = detect::Rtd3Probe::with_roots(
            &root.path().join("sys"),
            &root.path().join("proc"),
        );
        evaluate(&probe, None);
        assert!(status().expect("evaluated").gpu_clients.is_none());

        record_gpu_clients(GpuClientObservation {
            source: GpuClientSource::DeviceNodes,
            graphics: Vec::new(),
            compute: Vec::new(),
            device_node_holders: vec![client(4242, "some-game")],
            ignored_clients: Vec::new(),
        });
        let clients = status()
            .expect("evaluated")
            .gpu_clients
            .expect("sample recorded");
        assert_eq!(clients.source, "device-nodes");
        assert_eq!(clients.device_node_holders[0].pid, 4242);
        assert_eq!(clients.device_node_holders[0].name.as_deref(), Some("some-game"));
        assert!(clients.age_s >= 0.0);
        reset_for_test();
    }
}
