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

fn watch_loop(
    sup: &Arc<Mutex<Supervisor>>,
    probe: &Rtd3Probe,
    hint: Option<&str>,
    mut desktop_autostart_attempted: bool,
) {
    let mut active_ticks = 0u32;
    // One start attempt per active episode: a failed `profile::start` must not
    // retry at 1 Hz, and a dead engine retained in the profile slot must not
    // produce log spam. Reset when the GPU leaves `active`.
    let mut start_attempted = false;
    // Consecutive client-free ticks while an engine is attached; at the park
    // threshold the runtime is parked so the GPU can reach D3cold.
    let mut idle_ticks = 0u32;

    loop {
        super::evaluate(probe, hint);

        if super::protects_deep_sleep() {
            let released = gpu_rpc::release_idle_backends(RPC_BACKEND_IDLE_TTL);
            if released > 0 {
                logging::info(&format!(
                    "deep sleep: released {released} idle GPU backend(s) so the GPU can suspend"
                ));
            }
            mobile_tick(sup, &mut active_ticks, &mut start_attempted, &mut idle_ticks);
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
fn mobile_tick(
    sup: &Arc<Mutex<Supervisor>>,
    active_ticks: &mut u32,
    start_attempted: &mut bool,
    idle_ticks: &mut u32,
) {
    // A scan/verification child owns the GPU exclusively; its own
    // `/dev/nvidia*` fds and activity must never satisfy the start policy —
    // starting the engine here would race the child's raw GPU writes.
    if supervisor::active_child_kind(sup).is_some() {
        super::set_autostart_deferred(false);
        *active_ticks = 0;
        *start_attempted = false;
        *idle_ticks = 0;
        return;
    }
    // The profile slot (running OR retained-after-failure) means the runtime
    // was already handled; a retained failed engine deliberately blocks
    // restarts, so it must not count as "deferred" either.
    if supervisor::profile_slot_occupied(sup) {
        super::set_autostart_deferred(false);
        *active_ticks = 0;
        *start_attempted = false;
        if supervisor::profile_engine_running(sup) {
            super::set_parked(false);
            // Park policy: the engine's own NVML polling resets the driver's
            // idle timer forever, so an attached engine means the GPU can
            // NEVER suspend on its own. When no other process really uses the
            // GPU for the whole window, release it: the profile stays a
            // standing intent (persisted state kept) and reapplies at the
            // next real use.
            if gpu_in_use_by_client(supervisor::profile_gpu_index(sup)) {
                *idle_ticks = 0;
            } else {
                *idle_ticks += 1;
                if *idle_ticks >= park_after_idle_ticks() {
                    *idle_ticks = 0;
                    logging::info(
                        "deep sleep: no GPU clients for the idle window; parking the runtime",
                    );
                    if supervisor::park_runtime_for_deep_sleep(sup) {
                        super::set_parked(true);
                        // The parked intent is pending again from this very
                        // instant — a status read between ticks must not
                        // claim otherwise.
                        super::set_autostart_deferred(wakeable_runtime_pending());
                    }
                }
            }
        } else {
            *idle_ticks = 0;
        }
        return;
    }
    *idle_ticks = 0;

    let pending = wakeable_runtime_pending();
    super::set_autostart_deferred(pending);
    if !pending {
        *active_ticks = 0;
        *start_attempted = false;
        return;
    }

    if super::last_runtime_status() == Some(RuntimePmStatus::Active) {
        *active_ticks += 1;
    } else {
        *active_ticks = 0;
        *start_attempted = false;
    }
    if *active_ticks >= SUSTAINED_ACTIVE_TICKS
        && !*start_attempted
        && gpu_in_use_by_client(supervisor::deferred_runtime_gpu_index())
    {
        logging::info("deep sleep: GPU is in use; starting the deferred persisted runtime");
        if supervisor::start_autostart_if_configured(sup) {
            *start_attempted = true;
            super::set_autostart_deferred(false);
            super::set_parked(false);
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
fn gpu_in_use_by_client(gpu_index: Option<u32>) -> bool {
    if super::fine_grained_mode() {
        if let Some(index) = gpu_index {
            match gpu_rpc::context_pids(index) {
                Ok(pids) => {
                    let self_pid = std::process::id();
                    return pids.into_iter().any(|pid| pid != self_pid);
                }
                Err(error) => log_context_query_fallback(&error),
            }
        }
    }
    nvidia_client_present()
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

/// True when a process other than the daemon holds a `/dev/nvidia<N>` fd —
/// pure procfs reads, never touches the device. Root sees every process.
/// Test seam (never set in production): `PENGUIN_BURNERD_TEST_CLIENT_PROC`
/// points the scan at a fake proc tree so integration tests can stage and
/// remove GPU clients deterministically on any machine.
fn nvidia_client_present() -> bool {
    let proc_root = std::env::var_os("PENGUIN_BURNERD_TEST_CLIENT_PROC")
        .filter(|v| !v.is_empty())
        .map_or_else(|| PathBuf::from("/proc"), PathBuf::from);
    other_nvidia_client_in(proc_root, std::process::id())
}

/// Only the numbered render nodes count as a real GPU client. `nvidiactl`,
/// `nvidia-uvm*`, `nvidia-modeset`, and `nvidia-caps/*` are held long-lived
/// by monitoring/container tooling without keeping an RTD3 GPU awake.
fn is_nvidia_device_node(target: &str) -> bool {
    target
        .strip_prefix("/dev/nvidia")
        .is_some_and(|rest| !rest.is_empty() && rest.bytes().all(|b| b.is_ascii_digit()))
}

fn other_nvidia_client_in(proc_root: impl AsRef<Path>, self_pid: u32) -> bool {
    let Ok(entries) = fs::read_dir(proc_root) else {
        return false;
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
        for fd in fds.filter_map(Result::ok) {
            if let Ok(target) = fs::read_link(fd.path()) {
                if is_nvidia_device_node(&target.to_string_lossy()) {
                    return true;
                }
            }
        }
    }
    false
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
    fn detects_nvidia_fd_holders_and_skips_self() {
        let root = tempfile::tempdir().expect("tempdir");
        let fd_dir = root.path().join("4242").join("fd");
        fs::create_dir_all(&fd_dir).unwrap();
        symlink("/dev/nvidia0", fd_dir.join("7")).unwrap();
        fs::create_dir_all(root.path().join("not-a-pid")).unwrap();

        assert!(other_nvidia_client_in(root.path(), 1));
        // The daemon's own fds never count as a client.
        assert!(!other_nvidia_client_in(root.path(), 4242));
    }

    #[test]
    fn auxiliary_nvidia_nodes_are_not_clients() {
        let root = tempfile::tempdir().expect("tempdir");
        let fd_dir = root.path().join("100").join("fd");
        fs::create_dir_all(&fd_dir).unwrap();
        symlink("/dev/nvidiactl", fd_dir.join("3")).unwrap();
        symlink("/dev/nvidia-uvm", fd_dir.join("4")).unwrap();
        symlink("/dev/dri/renderD128", fd_dir.join("5")).unwrap();
        assert!(!other_nvidia_client_in(root.path(), 1));
    }
}
