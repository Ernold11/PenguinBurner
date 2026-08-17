//! Mode-aware GPU-client detection for the RTD3 watcher.
//!
//! The module hides both the client-evidence rules and the cadence of NVIDIA
//! queries. Fine-grained idle observations are latched in deferred mode so the
//! watcher cannot keep a GPU awake by periodically observing it; a latch that
//! survives [`LATCH_REPROBE_TICKS`] awake ticks expires into one fresh probe
//! so an already-latched holder that starts real work is still noticed.

use std::fs;
use std::path::{Path, PathBuf};

use crate::gpu::{self, GpuContextPids};
use crate::logging;

use super::detect::RuntimePmStatus;
use super::{GpuClientObservation, GpuClientProcess, GpuClientSource};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ClientPhase {
    Attached,
    Deferred,
}

#[derive(Debug, Clone, Copy)]
pub(crate) struct ClientTick {
    pub phase: ClientPhase,
    pub gpu_index: Option<u32>,
    pub fine_grained: bool,
    pub runtime_status: Option<RuntimePmStatus>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum ClientVerdict {
    Busy(GpuClientObservation),
    Idle(GpuClientObservation),
    /// No NVIDIA observation is safe or due. This never authorizes a start or
    /// advances a park countdown.
    Pending,
}

/// Latched ticks (1 Hz, counted only while awake and deferred) after which
/// one fresh probe re-arms. An already-latched fd holder that starts real GPU
/// work never changes the holder set and, with the GPU held awake by that
/// work, never produces the suspend/resume edge that clears the latch — so
/// without a bound the parked profile would never re-materialize for the
/// whole workload. A GPU that read `active` for this entire window is
/// demonstrably awake, so the one re-probe cannot wake anything.
const LATCH_REPROBE_TICKS: u32 = 300;

struct IdleLatch {
    gpu_index: Option<u32>,
    holders: Vec<GpuClientProcess>,
    /// Consecutive latch hits; at [`LATCH_REPROBE_TICKS`] the latch expires
    /// into one fresh probe.
    held_ticks: u32,
}

trait ContextProvider {
    fn context_pids(
        &self,
        gpu_index: u32,
    ) -> Result<GpuContextPids, gpu::context::ContextProbeError>;
}

struct EphemeralContextProvider;

impl ContextProvider for EphemeralContextProvider {
    fn context_pids(
        &self,
        gpu_index: u32,
    ) -> Result<GpuContextPids, gpu::context::ContextProbeError> {
        gpu::context::context_pids_ephemeral(gpu_index)
    }
}

pub(crate) struct ClientDetector {
    proc_root: PathBuf,
    self_pid: u32,
    provider: Box<dyn ContextProvider>,
    idle_latch: Option<IdleLatch>,
}

impl ClientDetector {
    pub(crate) fn system() -> Self {
        Self {
            proc_root: client_proc_root(),
            self_pid: std::process::id(),
            provider: Box::new(EphemeralContextProvider),
            idle_latch: None,
        }
    }

    pub(crate) fn tick(&mut self, input: ClientTick) -> ClientVerdict {
        // A running profile engine already owns and wakes the GPU even if the
        // cached sysfs sample still says `suspended`. Only a deferred runtime
        // relies on runtime_status to prove a query is safe.
        if input.phase == ClientPhase::Deferred
            && input.runtime_status != Some(RuntimePmStatus::Active)
        {
            if matches!(
                input.runtime_status,
                Some(RuntimePmStatus::Suspended | RuntimePmStatus::Resuming)
            ) {
                self.clear_idle_latch();
            }
            return ClientVerdict::Pending;
        }
        // Fine-grained deferred detection must query contexts for the exact
        // GPU. Without a resolved index, the global /dev/nvidiaN holder scan
        // cannot distinguish another card and must never authorize replay.
        if input.phase == ClientPhase::Deferred
            && input.fine_grained
            && input.gpu_index.is_none()
        {
            self.clear_idle_latch();
            return ClientVerdict::Pending;
        }

        let holders = nvidia_device_node_holders_in(&self.proc_root, self.self_pid);
        if input.phase == ClientPhase::Deferred && input.fine_grained {
            if let Some(latch) = self.idle_latch.as_mut() {
                if latch.gpu_index == input.gpu_index && latch.holders == holders {
                    latch.held_ticks = latch.held_ticks.saturating_add(1);
                    if latch.held_ticks < LATCH_REPROBE_TICKS {
                        return ClientVerdict::Pending;
                    }
                    // Bounded re-arm: the GPU stayed awake for the whole
                    // window, so probe once — a latched holder may have
                    // started real work that must re-materialize the profile.
                }
            }
        }
        self.clear_idle_latch();

        let observation = match self.observe(input.gpu_index, input.fine_grained, holders.clone()) {
            Ok(observation) => observation,
            Err(error) => {
                log_context_shutdown_failure(&error);
                if input.phase == ClientPhase::Deferred && input.fine_grained {
                    self.latch_idle(input.gpu_index, holders);
                }
                return ClientVerdict::Pending;
            }
        };
        if observation.in_use() {
            return ClientVerdict::Busy(observation);
        }

        if input.phase == ClientPhase::Deferred && input.fine_grained {
            self.latch_idle(input.gpu_index, observation.device_node_holders.clone());
        }
        ClientVerdict::Idle(observation)
    }

    fn observe(
        &self,
        gpu_index: Option<u32>,
        fine_grained: bool,
        device_node_holders: Vec<GpuClientProcess>,
    ) -> Result<GpuClientObservation, gpu::context::ContextProbeError> {
        let mut observation = GpuClientObservation {
            source: GpuClientSource::DeviceNodes,
            graphics: Vec::new(),
            compute: Vec::new(),
            device_node_holders,
            ignored_clients: Vec::new(),
        };
        if !fine_grained {
            return Ok(observation);
        }
        let Some(index) = gpu_index else {
            return Ok(observation);
        };
        match self.provider.context_pids(index) {
            Ok(contexts) => {
                observation.source = GpuClientSource::NvmlContexts;
                let (graphics, compute, ignored) = classified_context_processes(
                    &self.proc_root,
                    &contexts.graphics,
                    &contexts.compute,
                    self.self_pid,
                );
                observation.graphics = graphics;
                observation.compute = compute;
                observation.ignored_clients = ignored;
            }
            Err(gpu::context::ContextProbeError::Query(error)) => {
                log_context_query_fallback(&error);
                observation.ignored_clients = ignored_powerd_processes(
                    &self.proc_root,
                    &observation.device_node_holders,
                );
            }
            Err(error @ gpu::context::ContextProbeError::Shutdown { .. }) => {
                return Err(error);
            }
        }
        Ok(observation)
    }

    fn latch_idle(&mut self, gpu_index: Option<u32>, holders: Vec<GpuClientProcess>) {
        self.idle_latch = Some(IdleLatch {
            gpu_index,
            holders,
            held_ticks: 0,
        });
    }

    fn clear_idle_latch(&mut self) {
        self.idle_latch = None;
    }

    pub(crate) fn reset(&mut self) {
        self.clear_idle_latch();
    }

    #[cfg(test)]
    fn with_provider(
        proc_root: PathBuf,
        self_pid: u32,
        provider: impl ContextProvider + 'static,
    ) -> Self {
        Self {
            proc_root,
            self_pid,
            provider: Box::new(provider),
            idle_latch: None,
        }
    }
}

/// The proc tree the client scan and name lookups read. Test seam (never set
/// in production): `PENGUIN_BURNERD_TEST_CLIENT_PROC` points at a fake proc
/// tree so integration tests can stage GPU clients deterministically.
fn client_proc_root() -> PathBuf {
    std::env::var_os("PENGUIN_BURNERD_TEST_CLIENT_PROC")
        .filter(|value| !value.is_empty())
        .map_or_else(|| PathBuf::from("/proc"), PathBuf::from)
}

fn classified_context_processes(
    proc_root: &Path,
    graphics_pids: &[u32],
    compute_pids: &[u32],
    self_pid: u32,
) -> (
    Vec<GpuClientProcess>,
    Vec<GpuClientProcess>,
    Vec<GpuClientProcess>,
) {
    let mut unique_pids: Vec<u32> = graphics_pids
        .iter()
        .chain(compute_pids)
        .copied()
        .filter(|pid| *pid != self_pid)
        .collect();
    unique_pids.sort_unstable();
    unique_pids.dedup();

    let mut counted = Vec::new();
    let mut ignored = Vec::new();
    for pid in unique_pids {
        let name = process_name(proc_root, pid);
        let process = GpuClientProcess { pid, name };
        if is_root_nvidia_powerd(proc_root, &process) {
            ignored.push(process);
        } else {
            counted.push(process);
        }
    }
    let graphics = counted
        .iter()
        .filter(|process| graphics_pids.contains(&process.pid))
        .cloned()
        .collect();
    let compute = counted
        .iter()
        .filter(|process| compute_pids.contains(&process.pid))
        .cloned()
        .collect();
    (graphics, compute, ignored)
}

fn process_name(proc_root: &Path, pid: u32) -> Option<String> {
    let comm = fs::read_to_string(proc_root.join(pid.to_string()).join("comm")).ok()?;
    let name = comm.strip_suffix('\n').unwrap_or(&comm);
    (!name.is_empty()).then(|| name.to_string())
}

fn process_effective_uid(proc_root: &Path, pid: u32) -> Option<u32> {
    let status = fs::read_to_string(proc_root.join(pid.to_string()).join("status")).ok()?;
    let mut records = status.lines().filter_map(|line| line.strip_prefix("Uid:"));
    let record = records.next()?;
    if records.next().is_some() {
        return None;
    }
    let mut fields = record.split_whitespace();
    let _real: u32 = fields.next()?.parse().ok()?;
    let effective: u32 = fields.next()?.parse().ok()?;
    let _saved: u32 = fields.next()?.parse().ok()?;
    let _filesystem: u32 = fields.next()?.parse().ok()?;
    if fields.next().is_some() {
        return None;
    }
    Some(effective)
}

fn is_root_nvidia_powerd(proc_root: &Path, process: &GpuClientProcess) -> bool {
    process.name.as_deref() == Some("nvidia-powerd")
        && process_effective_uid(proc_root, process.pid) == Some(0)
}

fn ignored_powerd_processes(
    proc_root: &Path,
    processes: &[GpuClientProcess],
) -> Vec<GpuClientProcess> {
    processes
        .iter()
        .filter(|process| is_root_nvidia_powerd(proc_root, process))
        .cloned()
        .collect()
}

fn is_nvidia_device_node(target: &str) -> bool {
    target
        .strip_prefix("/dev/nvidia")
        .is_some_and(|rest| !rest.is_empty() && rest.bytes().all(|byte| byte.is_ascii_digit()))
}

fn nvidia_device_node_holders_in(
    proc_root: impl AsRef<Path>,
    self_pid: u32,
) -> Vec<GpuClientProcess> {
    let proc_root = proc_root.as_ref();
    let mut holders = Vec::new();
    let Ok(entries) = fs::read_dir(proc_root) else {
        return holders;
    };
    for entry in entries.filter_map(Result::ok) {
        let name = entry.file_name();
        let Some(pid) = name.to_str().and_then(|value| value.parse::<u32>().ok()) else {
            continue;
        };
        if pid == self_pid {
            continue;
        }
        let Ok(fds) = fs::read_dir(entry.path().join("fd")) else {
            continue;
        };
        let holds_device_node = fds.filter_map(Result::ok).any(|fd| {
            fs::read_link(fd.path())
                .is_ok_and(|target| is_nvidia_device_node(&target.to_string_lossy()))
        });
        if holds_device_node {
            holders.push(GpuClientProcess {
                pid,
                name: process_name(proc_root, pid),
            });
        }
    }
    holders.sort_unstable_by_key(|process| process.pid);
    holders
}

fn log_context_query_fallback(error: &impl std::fmt::Display) {
    static LOGGED: std::sync::Once = std::sync::Once::new();
    LOGGED.call_once(|| {
        logging::info(&format!(
            "deep sleep: NVML context query unavailable ({error}); using the device-node scan"
        ));
    });
}

fn log_context_shutdown_failure(error: &impl std::fmt::Display) {
    static LOGGED: std::sync::Once = std::sync::Once::new();
    LOGGED.call_once(|| {
        logging::info(&format!(
            "deep sleep: NVIDIA context probe cleanup failed ({error}); refusing an idle decision"
        ));
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;
    use std::os::unix::fs::symlink;
    use std::rc::Rc;

    struct ScriptedProvider {
        calls: Rc<RefCell<u32>>,
        contexts: Rc<RefCell<GpuContextPids>>,
    }

    struct ShutdownFailingProvider {
        calls: Rc<RefCell<u32>>,
    }

    impl ContextProvider for ScriptedProvider {
        fn context_pids(
            &self,
            _gpu_index: u32,
        ) -> Result<GpuContextPids, gpu::context::ContextProbeError> {
            *self.calls.borrow_mut() += 1;
            Ok(self.contexts.borrow().clone())
        }
    }

    impl ContextProvider for ShutdownFailingProvider {
        fn context_pids(
            &self,
            _gpu_index: u32,
        ) -> Result<GpuContextPids, gpu::context::ContextProbeError> {
            *self.calls.borrow_mut() += 1;
            Err(gpu::context::ContextProbeError::Shutdown {
                shutdown: gpu::GpuError::other("mock nvmlShutdown failure", 0),
                query: None,
            })
        }
    }

    fn active_tick(phase: ClientPhase) -> ClientTick {
        ClientTick {
            phase,
            gpu_index: Some(0),
            fine_grained: true,
            runtime_status: Some(RuntimePmStatus::Active),
        }
    }

    fn write_identity(root: &Path, pid: u32, name: &str, uid: u32) {
        let process = root.join(pid.to_string());
        fs::create_dir_all(&process).unwrap();
        fs::write(process.join("comm"), format!("{name}\n")).unwrap();
        fs::write(
            process.join("status"),
            format!("Name:\t{name}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n"),
        )
        .unwrap();
    }

    #[test]
    fn powerd_only_latches_idle_and_holder_change_rearms_probe() {
        let root = tempfile::tempdir().unwrap();
        write_identity(root.path(), 880, "nvidia-powerd", 0);
        let calls = Rc::new(RefCell::new(0));
        let contexts = Rc::new(RefCell::new(GpuContextPids {
            graphics: vec![880],
            compute: vec![880],
        }));
        let provider = ScriptedProvider {
            calls: calls.clone(),
            contexts: contexts.clone(),
        };
        let mut detector = ClientDetector::with_provider(root.path().to_path_buf(), 999, provider);
        let first = detector.tick(active_tick(ClientPhase::Deferred));
        assert!(matches!(first, ClientVerdict::Idle(_)));
        assert_eq!(*calls.borrow(), 1);
        assert_eq!(
            detector.tick(active_tick(ClientPhase::Deferred)),
            ClientVerdict::Pending
        );
        assert_eq!(*calls.borrow(), 1);

        write_identity(root.path(), 4242, "some-game", 1000);
        let fd_dir = root.path().join("4242/fd");
        fs::create_dir_all(&fd_dir).unwrap();
        symlink("/dev/nvidia0", fd_dir.join("7")).unwrap();
        contexts.borrow_mut().graphics.push(4242);
        let changed = detector.tick(active_tick(ClientPhase::Deferred));
        assert!(matches!(changed, ClientVerdict::Busy(_)));
        assert_eq!(*calls.borrow(), 2);
    }

    #[test]
    fn nonactive_status_never_opens_the_context_provider() {
        let root = tempfile::tempdir().unwrap();
        let calls = Rc::new(RefCell::new(0));
        let provider = ScriptedProvider {
            calls: calls.clone(),
            contexts: Rc::new(RefCell::new(GpuContextPids::default())),
        };
        let mut detector = ClientDetector::with_provider(root.path().to_path_buf(), 999, provider);
        let mut input = active_tick(ClientPhase::Deferred);
        input.runtime_status = Some(RuntimePmStatus::Suspended);
        assert_eq!(detector.tick(input), ClientVerdict::Pending);
        assert_eq!(*calls.borrow(), 0);
    }

    #[test]
    fn unresolved_fine_grained_target_ignores_global_device_holders() {
        let root = tempfile::tempdir().unwrap();
        write_identity(root.path(), 4242, "other-gpu-game", 1000);
        let fd_dir = root.path().join("4242/fd");
        fs::create_dir_all(&fd_dir).unwrap();
        symlink("/dev/nvidia0", fd_dir.join("7")).unwrap();
        let calls = Rc::new(RefCell::new(0));
        let provider = ScriptedProvider {
            calls: calls.clone(),
            contexts: Rc::new(RefCell::new(GpuContextPids::default())),
        };
        let mut detector = ClientDetector::with_provider(root.path().to_path_buf(), 999, provider);
        let mut input = active_tick(ClientPhase::Deferred);
        input.gpu_index = None;
        assert_eq!(detector.tick(input), ClientVerdict::Pending);
        assert_eq!(*calls.borrow(), 0);
    }

    #[test]
    fn shutdown_failure_is_pending_and_does_not_create_a_probe_loop() {
        let root = tempfile::tempdir().unwrap();
        let calls = Rc::new(RefCell::new(0));
        let provider = ShutdownFailingProvider {
            calls: calls.clone(),
        };
        let mut detector = ClientDetector::with_provider(root.path().to_path_buf(), 999, provider);
        assert_eq!(
            detector.tick(active_tick(ClientPhase::Deferred)),
            ClientVerdict::Pending
        );
        assert_eq!(
            detector.tick(active_tick(ClientPhase::Deferred)),
            ClientVerdict::Pending
        );
        assert_eq!(*calls.borrow(), 1);
    }

    #[test]
    fn unchanged_holder_set_does_not_reprobe_within_the_bounded_window() {
        let root = tempfile::tempdir().unwrap();
        write_identity(root.path(), 880, "nvidia-powerd", 0);
        let calls = Rc::new(RefCell::new(0));
        let contexts = Rc::new(RefCell::new(GpuContextPids {
            graphics: vec![880],
            compute: Vec::new(),
        }));
        let provider = ScriptedProvider {
            calls: calls.clone(),
            contexts: contexts.clone(),
        };
        let mut detector = ClientDetector::with_provider(root.path().to_path_buf(), 999, provider);
        assert!(matches!(
            detector.tick(active_tick(ClientPhase::Deferred)),
            ClientVerdict::Idle(_)
        ));

        write_identity(root.path(), 4242, "some-game", 1000);
        contexts.borrow_mut().graphics.push(4242);
        for _ in 0..(LATCH_REPROBE_TICKS - 1) {
            assert_eq!(
                detector.tick(active_tick(ClientPhase::Deferred)),
                ClientVerdict::Pending
            );
        }
        assert_eq!(*calls.borrow(), 1);
    }

    #[test]
    fn latched_holder_that_starts_work_is_noticed_at_the_reprobe_bound() {
        // The missed-wake gap: an already-listed fd holder starts submitting
        // real GPU work. The holder set never changes and the busy GPU never
        // suspends, so neither re-arm edge fires — only the bounded window
        // may notice the new context and re-materialize the parked profile.
        let root = tempfile::tempdir().unwrap();
        write_identity(root.path(), 1450, "plasmashell", 1000);
        let fd_dir = root.path().join("1450/fd");
        fs::create_dir_all(&fd_dir).unwrap();
        symlink("/dev/nvidia0", fd_dir.join("7")).unwrap();
        let calls = Rc::new(RefCell::new(0));
        let contexts = Rc::new(RefCell::new(GpuContextPids::default()));
        let provider = ScriptedProvider {
            calls: calls.clone(),
            contexts: contexts.clone(),
        };
        let mut detector = ClientDetector::with_provider(root.path().to_path_buf(), 999, provider);
        assert!(matches!(
            detector.tick(active_tick(ClientPhase::Deferred)),
            ClientVerdict::Idle(_)
        ));

        contexts.borrow_mut().graphics.push(1450);
        for _ in 0..(LATCH_REPROBE_TICKS - 1) {
            assert_eq!(
                detector.tick(active_tick(ClientPhase::Deferred)),
                ClientVerdict::Pending
            );
        }
        assert!(matches!(
            detector.tick(active_tick(ClientPhase::Deferred)),
            ClientVerdict::Busy(_)
        ));
        assert_eq!(*calls.borrow(), 2);
    }

    #[test]
    fn idle_reprobe_at_the_bound_relatches_for_another_window() {
        let root = tempfile::tempdir().unwrap();
        write_identity(root.path(), 880, "nvidia-powerd", 0);
        let calls = Rc::new(RefCell::new(0));
        let provider = ScriptedProvider {
            calls: calls.clone(),
            contexts: Rc::new(RefCell::new(GpuContextPids::default())),
        };
        let mut detector = ClientDetector::with_provider(root.path().to_path_buf(), 999, provider);
        assert!(matches!(
            detector.tick(active_tick(ClientPhase::Deferred)),
            ClientVerdict::Idle(_)
        ));
        for _ in 0..(LATCH_REPROBE_TICKS - 1) {
            assert_eq!(
                detector.tick(active_tick(ClientPhase::Deferred)),
                ClientVerdict::Pending
            );
        }
        // Bound reached: one probe, still idle, and the fresh latch holds the
        // next window without probing.
        assert!(matches!(
            detector.tick(active_tick(ClientPhase::Deferred)),
            ClientVerdict::Idle(_)
        ));
        assert_eq!(*calls.borrow(), 2);
        assert_eq!(
            detector.tick(active_tick(ClientPhase::Deferred)),
            ClientVerdict::Pending
        );
        assert_eq!(*calls.borrow(), 2);
    }

    #[test]
    fn runtime_pm_cycle_rearms_probe_without_a_new_holder() {
        let root = tempfile::tempdir().unwrap();
        write_identity(root.path(), 880, "nvidia-powerd", 0);
        let calls = Rc::new(RefCell::new(0));
        let contexts = Rc::new(RefCell::new(GpuContextPids {
            graphics: vec![880],
            compute: Vec::new(),
        }));
        let provider = ScriptedProvider {
            calls: calls.clone(),
            contexts: contexts.clone(),
        };
        let mut detector = ClientDetector::with_provider(root.path().to_path_buf(), 999, provider);
        assert!(matches!(
            detector.tick(active_tick(ClientPhase::Deferred)),
            ClientVerdict::Idle(_)
        ));

        let mut suspended = active_tick(ClientPhase::Deferred);
        suspended.runtime_status = Some(RuntimePmStatus::Suspended);
        assert_eq!(detector.tick(suspended), ClientVerdict::Pending);
        write_identity(root.path(), 4242, "some-game", 1000);
        contexts.borrow_mut().graphics.push(4242);
        assert!(matches!(
            detector.tick(active_tick(ClientPhase::Deferred)),
            ClientVerdict::Busy(_)
        ));
        assert_eq!(*calls.borrow(), 2);
    }

    #[test]
    fn unproven_nonactive_statuses_do_not_rearm_probe() {
        let root = tempfile::tempdir().unwrap();
        let calls = Rc::new(RefCell::new(0));
        let provider = ScriptedProvider {
            calls: calls.clone(),
            contexts: Rc::new(RefCell::new(GpuContextPids::default())),
        };
        let mut detector = ClientDetector::with_provider(root.path().to_path_buf(), 999, provider);
        assert!(matches!(
            detector.tick(active_tick(ClientPhase::Deferred)),
            ClientVerdict::Idle(_)
        ));

        for status in [
            RuntimePmStatus::Suspending,
            RuntimePmStatus::Error,
            RuntimePmStatus::Unsupported,
            RuntimePmStatus::Unknown,
        ] {
            let mut nonactive = active_tick(ClientPhase::Deferred);
            nonactive.runtime_status = Some(status);
            assert_eq!(detector.tick(nonactive), ClientVerdict::Pending);
            assert_eq!(
                detector.tick(active_tick(ClientPhase::Deferred)),
                ClientVerdict::Pending
            );
        }
        assert_eq!(*calls.borrow(), 1);
    }

    #[test]
    fn idle_latch_does_not_cross_gpu_indices() {
        let root = tempfile::tempdir().unwrap();
        let calls = Rc::new(RefCell::new(0));
        let provider = ScriptedProvider {
            calls: calls.clone(),
            contexts: Rc::new(RefCell::new(GpuContextPids::default())),
        };
        let mut detector = ClientDetector::with_provider(root.path().to_path_buf(), 999, provider);
        assert!(matches!(
            detector.tick(active_tick(ClientPhase::Deferred)),
            ClientVerdict::Idle(_)
        ));

        let mut other_gpu = active_tick(ClientPhase::Deferred);
        other_gpu.gpu_index = Some(1);
        assert!(matches!(detector.tick(other_gpu), ClientVerdict::Idle(_)));
        assert_eq!(*calls.borrow(), 2);
    }

    #[test]
    fn only_numbered_device_nodes_count_as_clients() {
        assert!(is_nvidia_device_node("/dev/nvidia0"));
        assert!(is_nvidia_device_node("/dev/nvidia12"));
        assert!(!is_nvidia_device_node("/dev/nvidiactl"));
        assert!(!is_nvidia_device_node("/dev/nvidia-uvm"));
        assert!(!is_nvidia_device_node("/dev/nvidia-modeset"));
        assert!(!is_nvidia_device_node("/dev/nvidia-caps/nvidia-cap1"));
        assert!(!is_nvidia_device_node("/dev/dri/renderD128"));
    }

    #[test]
    fn collects_nvidia_fd_holders_with_names_and_skips_self() {
        let root = tempfile::tempdir().unwrap();
        let fd_dir = root.path().join("4242/fd");
        fs::create_dir_all(&fd_dir).unwrap();
        symlink("/dev/nvidia0", fd_dir.join("7")).unwrap();
        fs::write(root.path().join("4242/comm"), "some-game\n").unwrap();
        let holders = nvidia_device_node_holders_in(root.path(), 1);
        assert_eq!(holders.len(), 1);
        assert_eq!(holders[0].pid, 4242);
        assert_eq!(holders[0].name.as_deref(), Some("some-game"));
        assert!(nvidia_device_node_holders_in(root.path(), 4242).is_empty());
    }

    #[test]
    fn classified_context_processes_drop_self_and_ignore_only_root_powerd() {
        let root = tempfile::tempdir().unwrap();
        write_identity(root.path(), 880, "nvidia-powerd", 0);
        write_identity(root.path(), 881, "nvidia-powerd", 1000);
        write_identity(root.path(), 4242, "some-game", 1000);
        let (graphics, compute, ignored) = classified_context_processes(
            root.path(),
            &[880, 881, 4242, 999],
            &[4242, 777],
            999,
        );
        assert_eq!(ignored.iter().map(|p| p.pid).collect::<Vec<_>>(), vec![880]);
        assert_eq!(graphics.iter().map(|p| p.pid).collect::<Vec<_>>(), vec![881, 4242]);
        assert_eq!(compute.iter().map(|p| p.pid).collect::<Vec<_>>(), vec![777, 4242]);
    }
}
