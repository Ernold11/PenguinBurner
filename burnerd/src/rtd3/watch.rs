//! Startup supervisor for the deep-sleep gate.
//!
//! `startup` replaces the unconditional boot-time autostart: a machine that is
//! decidably a desktop (gate `Disabled`) starts the persisted runtime
//! synchronously exactly as before, and every machine then gets a watcher
//! thread that keeps the gate's view live for the daemon's lifetime — an
//! undecided verdict can resolve, a `disabled` verdict can flip once the user
//! enables runtime PM, and a `suspended` sighting arms the gate for good.
//!
//! While armed, the watcher polls the kernel's cached `runtime_status` (a
//! wake-free read) once per second and starts the deferred runtime only when
//! the GPU is demonstrably in use: sustained `active` AND another process
//! holding a `/dev/nvidia<N>` device-node fd. Bare `active` is not enough —
//! transient wakes (Vulkan/GL capability probes from arbitrary apps,
//! boot-time driver init) last seconds, and attaching on them would hold the
//! GPU awake, recreating the bug this module exists to fix (issue #30).
//! Auxiliary nodes (`nvidiactl`, `nvidia-uvm`, `nvidia-modeset`,
//! `nvidia-caps/*`) are deliberately not counted: monitoring agents and
//! container toolkits hold them long-lived without keeping a fine-grained
//! RTD3 GPU awake. While armed it also drops idle RPC backends so a one-off
//! GUI telemetry query cannot pin NVML forever.

use std::fs;
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use crate::gpu_rpc;
use crate::logging;
use crate::supervisor::{self, Supervisor};

use super::detect::{DeepSleepDecision, Rtd3Probe, RuntimePmStatus};

const ARMED_TICK: Duration = Duration::from_secs(1);
/// Cadence while the verdict is `Disabled`: only re-evaluation (the user may
/// enable runtime PM at runtime) and sticky-arming observation happen here.
const DISABLED_TICK: Duration = Duration::from_secs(60);
/// How long an undecided gate may stay undecided before falling back to the
/// classic path (device never appeared / driver never initialized).
const UNDECIDED_GRACE_TICKS: u32 = 30;
/// Consecutive `active` readings required before a wake counts as real.
const SUSTAINED_ACTIVE_TICKS: u32 = 3;
/// Idle TTL for lazily-opened RPC backends while the gate is armed.
const RPC_BACKEND_IDLE_TTL: Duration = Duration::from_secs(30);

/// Evaluate the gate, run the classic synchronous autostart when the machine
/// is decidably a desktop (preserving the start-before-socket-bind ordering),
/// and spawn the lifetime watcher in every case.
pub fn startup(sup: &Arc<Mutex<Supervisor>>) {
    let probe = Rtd3Probe::system();
    let hint = supervisor::persisted_pci_bus_id_hint();
    let decision = super::evaluate(&probe, hint.as_deref());

    let classic_started = matches!(decision, DeepSleepDecision::Disabled { .. });
    if classic_started {
        supervisor::start_autostart_if_configured(sup);
    }

    let sup = sup.clone();
    thread::Builder::new()
        .name("penguin-burner-rtd3".to_string())
        .spawn(move || watch_loop(&sup, &probe, hint.as_deref(), classic_started))
        .expect("spawn rtd3 watcher thread");
}

fn watch_loop(
    sup: &Arc<Mutex<Supervisor>>,
    probe: &Rtd3Probe,
    hint: Option<&str>,
    mut classic_started: bool,
) {
    let mut undecided_ticks = 0u32;
    let mut active_ticks = 0u32;
    // One start attempt per active episode: a failed `profile::start` must not
    // retry at 1 Hz, and a dead engine retained in the profile slot must not
    // produce log spam. Reset when the GPU leaves `active`.
    let mut start_attempted = false;

    loop {
        if let Some(addr) = super::pci_addr() {
            super::note_runtime_status(probe.runtime_pm_status(&addr));
        }

        if super::is_armed() {
            let released = gpu_rpc::release_idle_backends(RPC_BACKEND_IDLE_TTL);
            if released > 0 {
                logging::info(&format!(
                    "deep sleep: released {released} idle GPU backend(s) so the GPU can suspend"
                ));
            }
            armed_tick(sup, &mut active_ticks, &mut start_attempted);
            thread::sleep(ARMED_TICK);
            continue;
        }

        match super::current_decision() {
            Some(DeepSleepDecision::Disabled { .. }) => {
                super::set_autostart_deferred(false);
                if !classic_started {
                    // The verdict resolved to desktop after boot (device
                    // appeared during the undecided window): run the classic
                    // start it would have received at boot.
                    classic_started = true;
                    supervisor::start_autostart_if_configured(sup);
                }
                thread::sleep(DISABLED_TICK);
                super::evaluate(probe, hint);
            }
            _ => {
                undecided_ticks += 1;
                if !classic_started && undecided_ticks > UNDECIDED_GRACE_TICKS {
                    // Could not decide (driver power file stayed `?` or the
                    // device never appeared). Fall back to the classic start
                    // so a saved profile is not silently dropped; persistence
                    // mode stays suppressed while the gate is undecided, so
                    // this cannot permanently block D3cold on a laptop that
                    // was merely slow to initialize.
                    logging::warn(
                        "deep sleep gate stayed undecided; falling back to classic runtime start",
                    );
                    classic_started = true;
                    supervisor::start_autostart_if_configured(sup);
                }
                thread::sleep(ARMED_TICK);
                super::evaluate(probe, hint);
            }
        }
    }
}

/// One armed observation tick: hold the deferred runtime back until the GPU
/// is genuinely in use, and never fight another GPU owner.
fn armed_tick(sup: &Arc<Mutex<Supervisor>>, active_ticks: &mut u32, start_attempted: &mut bool) {
    // A scan/verification child owns the GPU exclusively; its own
    // `/dev/nvidia*` fds and activity must never satisfy the start policy —
    // starting the engine here would race the child's raw GPU writes.
    if supervisor::active_child_kind(sup).is_some() {
        super::set_autostart_deferred(false);
        *active_ticks = 0;
        *start_attempted = false;
        return;
    }
    // The profile slot (running OR retained-after-failure) means the runtime
    // was already handled; a retained failed engine deliberately blocks
    // restarts, so it must not count as "deferred" either.
    if supervisor::profile_slot_occupied(sup) {
        super::set_autostart_deferred(false);
        *active_ticks = 0;
        *start_attempted = false;
        return;
    }

    let pending = supervisor::deferred_runtime_pending();
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
    if *active_ticks >= SUSTAINED_ACTIVE_TICKS && !*start_attempted && nvidia_client_present() {
        *start_attempted = true;
        logging::info("deep sleep: GPU is in use; starting the deferred persisted runtime");
        super::set_autostart_deferred(false);
        supervisor::start_autostart_if_configured(sup);
    }
}

/// True when a process other than the daemon holds a `/dev/nvidia<N>` fd —
/// pure procfs reads, never touches the device. Root sees every process.
fn nvidia_client_present() -> bool {
    other_nvidia_client_in("/proc", std::process::id())
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
