//! Startup supervisor for the deep-sleep gate.
//!
//! `startup` replaces the unconditional boot-time autostart: Desktop mode
//! starts the persisted runtime synchronously exactly as before, and every
//! machine then gets a watcher thread that keeps the detected mode live for
//! the daemon's lifetime — Unknown can resolve, Desktop can flip once the user
//! enables runtime PM, and a `suspended` sighting selects Mobile handling for
//! good.
//!
//! In Mobile and Unknown modes, the watcher polls the kernel's cached
//! `runtime_status` (a wake-free read) once per second and starts the deferred
//! runtime only when the GPU is demonstrably in use: sustained `active` AND a
//! real GPU client. Bare `active` is not enough — transient wakes (Vulkan/GL
//! capability probes from arbitrary apps, boot-time driver init) last seconds,
//! and attaching on them would hold the GPU awake, recreating the bug this
//! module exists to fix (issue #30).
//!
//! What counts as a "real GPU client" depends on the driver's runtime-D3
//! granularity. Coarse-grained (and undecided) RTD3 keeps the GPU awake while
//! any process holds a `/dev/nvidia<N>` device-node fd, so the fd scan is the
//! accurate signal there; auxiliary nodes (`nvidiactl`, `nvidia-uvm`,
//! `nvidia-modeset`, `nvidia-caps/*`) are deliberately not counted. Under
//! fine-grained RTD3 the driver wakes per submitted work, not per open fd —
//! desktop shells and monitoring agents hold the render node open for hours
//! while the GPU sleeps — so an fd holder proves nothing, and treating it as
//! use would starve the park policy forever. There the watcher asks NVML for
//! live graphics/compute contexts instead, falling back to the fd scan only
//! when that query fails (driver too old for the _v3 symbols, no backend).
//! While protected it also drops idle RPC backends so a one-off GUI telemetry
//! query cannot pin NVML forever.

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use crate::gpu_rpc;
use crate::logging;
use crate::supervisor::{self, Supervisor};

use super::detect::{Rtd3Probe, RuntimePmStatus};
use super::{GpuClientObservation, GpuClientProcess, GpuClientSource};

const MOBILE_TICK: Duration = Duration::from_secs(1);
/// Cadence in Desktop mode: re-evaluate in case the user enables runtime PM.
const DESKTOP_TICK: Duration = Duration::from_secs(60);
/// Consecutive `active` readings required before a wake counts as real.
const SUSTAINED_ACTIVE_TICKS: u32 = 3;
/// Idle TTL for lazily-opened RPC backends in Mobile or Unknown mode.
const RPC_BACKEND_IDLE_TTL: Duration = Duration::from_secs(30);

/// Client-free seconds (one Mobile tick each) before a running engine is
/// parked so the GPU can suspend. Wide enough to ride out game restarts,
/// launcher hand-offs, and shader-compile gaps without a park/reattach
/// cycle. Shrunk by the integration-test timing env so the park path can be
/// exercised in seconds.
fn park_after_idle_ticks() -> u32 {
    if std::env::var_os("PENGUIN_BURNERD_TEST_TIMINGS").is_some_and(|v| !v.is_empty()) {
        2
    } else {
        60
    }
}

/// Evaluate the mode, run eager autostart only in definite Desktop mode
/// (preserving the start-before-socket-bind ordering), and spawn the lifetime
/// watcher in every case.
pub fn startup(sup: &Arc<Mutex<Supervisor>>) {
    let probe = Rtd3Probe::system();
    let hint = supervisor::persisted_pci_bus_id_hint();
    super::evaluate(&probe, hint.as_deref());

    let desktop_autostart_attempted = super::allows_desktop_autostart();
    if desktop_autostart_attempted {
        supervisor::start_autostart_if_configured(sup);
    }

    let sup = sup.clone();
    thread::Builder::new()
        .name("penguin-burner-rtd3".to_string())
        .spawn(move || watch_loop(&sup, &probe, hint.as_deref(), desktop_autostart_attempted))
        .expect("spawn rtd3 watcher thread");
}

/// The watcher's per-tick counters: one wake episode's progress and one park
/// countdown. Reset on the edges commented at each `mobile_tick` site.
#[derive(Default)]
struct TickState {
    /// Consecutive `active` readings; a wake counts as real at
    /// [`SUSTAINED_ACTIVE_TICKS`].
    active_ticks: u32,
    /// One start attempt per active episode: a failed `profile::start` must
    /// not retry at 1 Hz, and a dead engine retained in the profile slot must
    /// not produce log spam. Reset when the GPU leaves `active`.
    start_attempted: bool,
    /// The current active episode began from a `suspended` reading, so a
    /// sustained-active-without-clients outcome really is a transient wake.
    /// Without this gate the drain tail of every park would be mislabeled:
    /// the GPU stays `active` for the driver's autosuspend delay after the
    /// engine releases it, which is not a wake.
    woke_from_suspend: bool,
    /// One "transient wake" journal line per active episode.
    transient_wake_logged: bool,
    /// Consecutive client-free ticks while an engine is attached; at the park
    /// threshold the runtime is parked so the GPU can reach D3cold.
    idle_ticks: u32,
    /// The countdown-start journal line has announced the current continuous
    /// client-free stretch. Latched across a refused park (which restarts the
    /// countdown) so the announcement cannot re-fire every window.
    countdown_announced: bool,
    /// A refused park has explained itself for the current client-free
    /// stretch; further refusals stay quiet until the stretch ends.
    park_refusal_logged: bool,
}

impl TickState {
    /// Reset the wake-episode trackers (the park countdown is reset
    /// separately — the two run in disjoint phases).
    fn reset_wake_episode(&mut self) {
        self.active_ticks = 0;
        self.start_attempted = false;
        self.woke_from_suspend = false;
        self.transient_wake_logged = false;
    }

    /// End the current client-free stretch: zero the countdown and re-arm
    /// its journal latches.
    fn end_countdown(&mut self) {
        self.idle_ticks = 0;
        self.countdown_announced = false;
        self.park_refusal_logged = false;
    }

    /// End the countdown because its preconditions vanished (not because a
    /// client appeared). Logs only when a countdown was actually in
    /// progress, so an announced start line is never left dangling.
    fn abandon_countdown(&mut self, reason: &str) {
        if self.idle_ticks > 0 {
            logging::info(&format!("deep sleep: park countdown abandoned: {reason}"));
        }
        self.end_countdown();
    }
}

fn watch_loop(
    sup: &Arc<Mutex<Supervisor>>,
    probe: &Rtd3Probe,
    hint: Option<&str>,
    mut desktop_autostart_attempted: bool,
) {
    let mut tick_state = TickState::default();

    loop {
        super::evaluate(probe, hint);

        if super::protects_deep_sleep() {
            let released = gpu_rpc::release_idle_backends(RPC_BACKEND_IDLE_TTL);
            if released > 0 {
                logging::info(&format!(
                    "deep sleep: released {released} idle GPU backend(s) so the GPU can suspend"
                ));
            }
            mobile_tick(sup, &mut tick_state);
            thread::sleep(MOBILE_TICK);
            continue;
        }

        super::set_autostart_deferred(false);
        if !desktop_autostart_attempted && super::allows_desktop_autostart() {
            // Detection resolved from Unknown to Desktop after boot: make the
            // one eager-start attempt Desktop mode would have received at boot.
            desktop_autostart_attempted = true;
            supervisor::start_autostart_if_configured(sup);
        }
        thread::sleep(DESKTOP_TICK);
    }
}

/// One Mobile/Unknown observation tick: hold the deferred runtime back until
/// the GPU is genuinely in use, park an attached-but-idle runtime so the GPU
/// can suspend, and never fight another GPU owner.
fn mobile_tick(sup: &Arc<Mutex<Supervisor>>, state: &mut TickState) {
    // A scan/verification child owns the GPU exclusively; its own
    // `/dev/nvidia*` fds and activity must never satisfy the start policy —
    // starting the engine here would race the child's raw GPU writes.
    if supervisor::active_child_kind(sup).is_some() {
        super::set_autostart_deferred(false);
        state.reset_wake_episode();
        state.abandon_countdown("a scan/verification child took the GPU");
        return;
    }
    // The profile slot (running OR retained-after-failure) means the runtime
    // was already handled; a retained failed engine deliberately blocks
    // restarts, so it must not count as "deferred" either.
    if supervisor::profile_slot_occupied(sup) {
        super::set_autostart_deferred(false);
        state.reset_wake_episode();
        if supervisor::profile_engine_running(sup) {
            super::set_parked(false);
            // Park policy: the engine's own NVML polling resets the driver's
            // idle timer forever, so an attached engine means the GPU can
            // NEVER suspend on its own. When no other process really uses the
            // GPU for the whole window, release it: the profile stays a
            // standing intent (persisted state kept) and reapplies at the
            // next real use.
            if gpu_in_use_by_client(supervisor::profile_gpu_index(sup)) {
                if state.idle_ticks > 0 {
                    logging::info(&format!(
                        "deep sleep: park countdown reset after {}s: a GPU client appeared",
                        state.idle_ticks
                    ));
                }
                state.end_countdown();
            } else {
                state.idle_ticks += 1;
                if state.idle_ticks == 1 && !state.countdown_announced {
                    state.countdown_announced = true;
                    // Remaining time, not the window size: this tick already
                    // counted, so the park lands threshold-1 seconds after
                    // this line's own timestamp.
                    logging::info(&format!(
                        "deep sleep: no GPU clients; parking the runtime in {}s unless one appears",
                        park_after_idle_ticks() - state.idle_ticks
                    ));
                }
                if state.idle_ticks >= park_after_idle_ticks() {
                    state.idle_ticks = 0;
                    logging::info(
                        "deep sleep: no GPU clients for the idle window; parking the runtime",
                    );
                    if supervisor::park_runtime_for_deep_sleep(sup) {
                        state.end_countdown();
                        // Deferral first: the moment `parked` becomes
                        // visible, the status block must already say the
                        // runtime will reapply. In the other order a status
                        // read can land between the two gate writes (the
                        // deferral check reads spec files) and see a parked
                        // runtime that claims it will not come back.
                        super::set_autostart_deferred(wakeable_runtime_pending());
                        super::set_parked(true);
                    } else if !state.park_refusal_logged {
                        // Once per client-free stretch: without this line a
                        // refused park (game session owns the runtime, or
                        // the engine did not stop in time) reads as a
                        // countdown that silently went nowhere, repeating
                        // every window.
                        state.park_refusal_logged = true;
                        logging::info(
                            "deep sleep: park refused (a game session owns the runtime or the engine did not stop); retrying every idle window",
                        );
                    }
                }
            }
        } else {
            state.abandon_countdown("the runtime engine is no longer running");
        }
        return;
    }
    state.abandon_countdown("the runtime is no longer attached");

    let pending = wakeable_runtime_pending();
    super::set_autostart_deferred(pending);
    if !pending {
        state.reset_wake_episode();
        return;
    }

    if super::last_runtime_status() == Some(RuntimePmStatus::Active) {
        state.active_ticks += 1;
    } else {
        state.reset_wake_episode();
        // Only an episode that starts from suspension can be a wake. One that
        // starts while the GPU reads `active` is the drain tail of whatever
        // just released it (a park, an exiting child) — the driver holds the
        // GPU in D0 for its autosuspend delay after the last release.
        state.woke_from_suspend = matches!(
            super::last_runtime_status(),
            Some(RuntimePmStatus::Suspended | RuntimePmStatus::Resuming)
        );
    }
    if state.active_ticks >= SUSTAINED_ACTIVE_TICKS && !state.start_attempted {
        if gpu_in_use_by_client(supervisor::deferred_runtime_gpu_index()) {
            logging::info("deep sleep: GPU is in use; starting the deferred persisted runtime");
            if supervisor::start_autostart_if_configured(sup) {
                state.start_attempted = true;
                super::set_autostart_deferred(false);
                super::set_parked(false);
            }
        } else if state.woke_from_suspend && !state.transient_wake_logged {
            // Once per genuine wake episode: the GPU came out of suspend but
            // nothing holds a counted client, so the runtime stays deferred.
            // This is the journal's answer to "the GPU woke up — why didn't
            // the profile apply?": a capability probe, not use.
            state.transient_wake_logged = true;
            logging::info(
                "deep sleep: GPU awake but no counted clients; keeping the runtime deferred (transient wake?)",
            );
        }
    }
}

/// A runtime is pending AND worth waking for. A pending stock runtime
/// enforces nothing: attaching an engine for it would only pin the GPU, so
/// it neither counts as deferred nor wakes.
fn wakeable_runtime_pending() -> bool {
    supervisor::deferred_runtime_pending()
        && supervisor::deferred_runtime_mode().as_deref() != Some("stock")
}

/// True when another process is really using the GPU (the mode-aware client
/// signal described in the module docs). Only called while the GPU is already
/// awake — an attached engine (park check) or sustained `active` (wake check)
/// — so the fine-grained NVML query never wakes a sleeping GPU; the registry
/// backend it opens is swept by `release_idle_backends` once the checks stop.
///
/// Every call records the full sample (per-kind context holders AND
/// device-node holders, with process names) as the `deep_sleep.gpu_clients`
/// status block, so someone debugging a park that does or does not happen
/// can see exactly the evidence the decision used.
fn gpu_in_use_by_client(gpu_index: Option<u32>) -> bool {
    let proc_root = client_proc_root();
    let self_pid = std::process::id();
    // Sampled even when the decision ignores it: a shell holding
    // `/dev/nvidia0` open while the context lists stay empty is precisely
    // what explains a fine-grained park to a reader of the status block.
    let mut observation = GpuClientObservation {
        source: GpuClientSource::DeviceNodes,
        graphics: Vec::new(),
        compute: Vec::new(),
        device_node_holders: nvidia_device_node_holders_in(&proc_root, self_pid),
    };
    if super::fine_grained_mode() {
        if let Some(index) = gpu_index {
            match gpu_rpc::context_pids(index) {
                Ok(contexts) => {
                    observation.source = GpuClientSource::NvmlContexts;
                    observation.graphics = named_processes(&proc_root, &contexts.graphics, self_pid);
                    observation.compute = named_processes(&proc_root, &contexts.compute, self_pid);
                }
                Err(error) => log_context_query_fallback(&error),
            }
        }
    }
    // The verdict, the status block's `total_count`, and the journal line all
    // come from the same `counted_pids` definition, so they can never
    // disagree about who was counted.
    let in_use = observation.in_use();
    super::record_gpu_clients(observation);
    in_use
}

/// The NVML-context fallback is a per-boot property (old driver, missing
/// symbols); log it once, not at 1 Hz.
fn log_context_query_fallback(error: &str) {
    static LOGGED: std::sync::Once = std::sync::Once::new();
    LOGGED.call_once(|| {
        logging::info(&format!(
            "deep sleep: NVML context query unavailable ({error}); using the device-node scan"
        ));
    });
}

/// The proc tree the client scan and name lookups read. Test seam (never set
/// in production): `PENGUIN_BURNERD_TEST_CLIENT_PROC` points at a fake proc
/// tree so integration tests can stage and remove GPU clients
/// deterministically on any machine.
fn client_proc_root() -> PathBuf {
    std::env::var_os("PENGUIN_BURNERD_TEST_CLIENT_PROC")
        .filter(|v| !v.is_empty())
        .map_or_else(|| PathBuf::from("/proc"), PathBuf::from)
}

/// Resolve `/proc/<pid>/comm` names for context PIDs, dropping the daemon's
/// own PID so the lists match what the park/wake decision counts.
fn named_processes(proc_root: &Path, pids: &[u32], self_pid: u32) -> Vec<GpuClientProcess> {
    pids.iter()
        .copied()
        .filter(|pid| *pid != self_pid)
        .map(|pid| GpuClientProcess {
            pid,
            name: process_name(proc_root, pid),
        })
        .collect()
}

/// Process name from `<proc_root>/<pid>/comm`; `None` once the process is
/// gone. Pure procfs read — never touches the GPU.
fn process_name(proc_root: &Path, pid: u32) -> Option<String> {
    let comm = fs::read_to_string(proc_root.join(pid.to_string()).join("comm")).ok()?;
    let name = comm.trim();
    (!name.is_empty()).then(|| name.to_string())
}

/// Only the numbered render nodes count as a real GPU client. `nvidiactl`,
/// `nvidia-uvm*`, `nvidia-modeset`, and `nvidia-caps/*` are held long-lived
/// by monitoring/container tooling without keeping an RTD3 GPU awake.
fn is_nvidia_device_node(target: &str) -> bool {
    target
        .strip_prefix("/dev/nvidia")
        .is_some_and(|rest| !rest.is_empty() && rest.bytes().all(|b| b.is_ascii_digit()))
}

/// Every process other than the daemon holding a `/dev/nvidia<N>` fd, with
/// names — pure procfs reads, never touches the device. Root sees every
/// process. Sorted by PID so repeated status reads stay stable.
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
        let Some(pid) = name.to_str().and_then(|n| n.parse::<u32>().ok()) else {
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;

    #[test]
    fn only_numbered_device_nodes_count_as_clients() {
        assert!(is_nvidia_device_node("/dev/nvidia0"));
        assert!(is_nvidia_device_node("/dev/nvidia12"));
        assert!(!is_nvidia_device_node("/dev/nvidiactl"));
        assert!(!is_nvidia_device_node("/dev/nvidia-uvm"));
        assert!(!is_nvidia_device_node("/dev/nvidia-uvm-tools"));
        assert!(!is_nvidia_device_node("/dev/nvidia-modeset"));
        assert!(!is_nvidia_device_node("/dev/nvidia-caps/nvidia-cap1"));
        assert!(!is_nvidia_device_node("/dev/nvidia"));
        assert!(!is_nvidia_device_node("/dev/dri/renderD128"));
    }

    #[test]
    fn collects_nvidia_fd_holders_with_names_and_skips_self() {
        let root = tempfile::tempdir().expect("tempdir");
        let fd_dir = root.path().join("4242").join("fd");
        fs::create_dir_all(&fd_dir).unwrap();
        symlink("/dev/nvidia0", fd_dir.join("7")).unwrap();
        fs::write(root.path().join("4242").join("comm"), "some-game\n").unwrap();
        // A holder whose comm is unreadable still appears, without a name.
        let anon_fd_dir = root.path().join("77").join("fd");
        fs::create_dir_all(&anon_fd_dir).unwrap();
        symlink("/dev/nvidia1", anon_fd_dir.join("3")).unwrap();
        fs::create_dir_all(root.path().join("not-a-pid")).unwrap();

        let holders = nvidia_device_node_holders_in(root.path(), 1);
        assert_eq!(holders.len(), 2);
        assert_eq!(holders[0].pid, 77);
        assert_eq!(holders[0].name, None);
        assert_eq!(holders[1].pid, 4242);
        assert_eq!(holders[1].name.as_deref(), Some("some-game"));
        // The daemon's own fds never count as a client.
        let holders = nvidia_device_node_holders_in(root.path(), 4242);
        assert_eq!(holders.len(), 1);
        assert_eq!(holders[0].pid, 77);
    }

    #[test]
    fn auxiliary_nvidia_nodes_are_not_clients() {
        let root = tempfile::tempdir().expect("tempdir");
        let fd_dir = root.path().join("100").join("fd");
        fs::create_dir_all(&fd_dir).unwrap();
        symlink("/dev/nvidiactl", fd_dir.join("3")).unwrap();
        symlink("/dev/nvidia-uvm", fd_dir.join("4")).unwrap();
        symlink("/dev/dri/renderD128", fd_dir.join("5")).unwrap();
        assert!(nvidia_device_node_holders_in(root.path(), 1).is_empty());
    }

    #[test]
    fn named_processes_resolve_comm_and_drop_the_daemon() {
        let root = tempfile::tempdir().expect("tempdir");
        fs::create_dir_all(root.path().join("123")).unwrap();
        fs::write(root.path().join("123").join("comm"), "cuda-worker\n").unwrap();

        let named = named_processes(root.path(), &[123, 456, 999], 456);
        assert_eq!(named.len(), 2);
        assert_eq!(named[0].pid, 123);
        assert_eq!(named[0].name.as_deref(), Some("cuda-worker"));
        // A PID that exited between the NVML query and the lookup keeps its
        // entry so the counts stay honest, just without a name.
        assert_eq!(named[1].pid, 999);
        assert_eq!(named[1].name, None);
    }
}
