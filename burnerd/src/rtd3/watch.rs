//! Startup supervisor for the deep-sleep gate.
//!
//! `startup` replaces the unconditional boot-time autostart: desktops (gate
//! `Disabled`) start the persisted runtime synchronously exactly as before;
//! RTD3 machines defer it and hand control to a watcher thread that polls the
//! kernel's cached `runtime_status` (a wake-free read) once per second.
//!
//! The watcher starts the deferred runtime only when the GPU is demonstrably
//! in use: sustained `active` AND some other process holding a `/dev/nvidia*`
//! file descriptor. Bare `active` is not enough — transient wakes (Vulkan/GL
//! capability probes from arbitrary apps, boot-time driver init) last seconds
//! and attaching on them would hold the GPU awake, recreating the bug this
//! module exists to fix (issue #30). While armed it also drops idle RPC
//! backends so a one-off GUI telemetry query cannot pin NVML forever.

use std::fs;
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use crate::gpu_rpc;
use crate::logging;
use crate::supervisor::{self, Supervisor};

use super::detect::{DeepSleepDecision, Rtd3Probe, RuntimePmStatus};

const TICK: Duration = Duration::from_secs(1);
/// How long an undecided gate may stay undecided before falling back to the
/// classic path (device never appeared / driver never initialized).
const UNDECIDED_GRACE_TICKS: u32 = 30;
/// Consecutive `active` readings required before the wake counts as real.
const SUSTAINED_ACTIVE_TICKS: u32 = 3;
/// Idle TTL for lazily-opened RPC backends while the gate is armed.
const RPC_BACKEND_IDLE_TTL: Duration = Duration::from_secs(30);

/// Evaluate the gate and either run the classic synchronous autostart
/// (desktops) or spawn the deep-sleep watcher (armed / undecided).
pub fn startup(sup: &Arc<Mutex<Supervisor>>) {
    let probe = Rtd3Probe::system();
    let hint = supervisor::persisted_pci_bus_id_hint();
    let decision = super::evaluate(&probe, hint.as_deref());

    if matches!(decision, DeepSleepDecision::Disabled { .. }) {
        supervisor::start_autostart_if_configured(sup);
        return;
    }

    let sup = sup.clone();
    thread::Builder::new()
        .name("penguin-burner-rtd3".to_string())
        .spawn(move || watch_loop(&sup, &probe, hint.as_deref()))
        .expect("spawn rtd3 watcher thread");
}

fn watch_loop(sup: &Arc<Mutex<Supervisor>>, probe: &Rtd3Probe, hint: Option<&str>) {
    // Phase 1: resolve an undecided gate. Stay detached while waiting — the
    // whole point is that "don't know yet" must not attach NVML.
    let mut undecided_ticks = 0u32;
    loop {
        match super::current_decision() {
            Some(DeepSleepDecision::Armed { .. }) => break,
            Some(DeepSleepDecision::Disabled { .. }) => {
                supervisor::start_autostart_if_configured(sup);
                return;
            }
            _ => {
                if let Some(addr) = super::pci_addr() {
                    super::note_runtime_status(probe.runtime_pm_status(&addr));
                    if super::is_armed() {
                        // Sticky suspended observation resolved it for us.
                        break;
                    }
                }
                undecided_ticks += 1;
                if undecided_ticks > UNDECIDED_GRACE_TICKS {
                    logging::warn(
                        "deep sleep gate stayed undecided; falling back to classic runtime start",
                    );
                    supervisor::start_autostart_if_configured(sup);
                    return;
                }
                super::evaluate(probe, hint);
            }
        }
        thread::sleep(TICK);
    }

    // Phase 2: armed. Defer the persisted runtime until the GPU is really in
    // use, and keep sweeping idle RPC backends so nothing pins the GPU.
    let mut active_ticks = 0u32;
    loop {
        let released = gpu_rpc::release_idle_backends(RPC_BACKEND_IDLE_TTL);
        if released > 0 {
            logging::info(&format!(
                "deep sleep: released {released} idle GPU backend(s) so the GPU can suspend"
            ));
        }

        let Some(addr) = super::pci_addr() else {
            thread::sleep(TICK);
            continue;
        };
        let status = probe.runtime_pm_status(&addr);
        super::note_runtime_status(status);

        if supervisor::profile_engine_running(sup) {
            super::set_autostart_deferred(false);
            active_ticks = 0;
            thread::sleep(TICK);
            continue;
        }

        let pending = supervisor::has_persisted_runtime();
        super::set_autostart_deferred(pending);
        if pending {
            active_ticks = if status == RuntimePmStatus::Active {
                active_ticks + 1
            } else {
                0
            };
            if active_ticks >= SUSTAINED_ACTIVE_TICKS && nvidia_client_present() {
                logging::info(
                    "deep sleep: GPU is in use; starting the deferred persisted runtime",
                );
                super::set_autostart_deferred(false);
                supervisor::start_autostart_if_configured(sup);
                active_ticks = 0;
            }
        } else {
            active_ticks = 0;
        }
        thread::sleep(TICK);
    }
}

/// True when a process other than the daemon holds a `/dev/nvidia*` fd —
/// pure procfs reads, never touches the device. Root sees every process.
fn nvidia_client_present() -> bool {
    other_nvidia_client_in("/proc", std::process::id())
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
                if target.to_string_lossy().starts_with("/dev/nvidia") {
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
    fn no_client_when_fd_targets_are_unrelated() {
        let root = tempfile::tempdir().expect("tempdir");
        let fd_dir = root.path().join("100").join("fd");
        fs::create_dir_all(&fd_dir).unwrap();
        symlink("/dev/dri/renderD128", fd_dir.join("3")).unwrap();
        assert!(!other_nvidia_client_in(root.path(), 1));
    }
}
