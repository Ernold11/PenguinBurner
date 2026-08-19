//! Startup supervisor for the deep-sleep gate.
//!
//! `startup` replaces the unconditional boot-time autostart: Desktop mode
//! starts the persisted runtime synchronously exactly as before, and every
//! machine then gets a watcher thread that keeps the detected mode live for
//! the daemon's lifetime — Unknown can resolve, Desktop can flip once the user
//! enables runtime PM, and a `suspended` sighting selects Mobile handling for
//! that GPU until the active runtime target changes.
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
//! live graphics/compute contexts instead. A root-owned `nvidia-powerd`
//! context is reported but excluded: Dynamic Boost is driver infrastructure,
//! and a real workload alongside it still has its own counted context. The
//! context query owns one short-lived NVML-only session and closes it before
//! returning. An idle result latches until a changed numbered-node holder set,
//! a runtime-PM sleep/wake cycle, or a bounded stretch of awake latched ticks
//! re-arms the query, so observation cannot become a periodic self-sustaining
//! GPU hold — while a latched holder that starts real work is still noticed.
//! The watcher falls back to the fd scan only when the context query fails
//! (driver too old for the _v3 symbols, no backend), applying the same narrow
//! helper exclusion in confirmed fine-grained mode.
//! While protected it also drops idle RPC backends so a one-off GUI telemetry
//! query cannot pin NVML forever.

use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use crate::gpu_rpc;
use crate::logging;
use crate::supervisor::{self, Supervisor};

use super::clients::{ClientDetector, ClientPhase, ClientTick, ClientVerdict};
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
    let hint = runtime_pci_hint(sup, &probe);
    super::evaluate(&probe, hint.as_deref());

    let desktop_autostart_attempted = super::allows_desktop_autostart();
    if desktop_autostart_attempted {
        supervisor::start_autostart_if_configured(sup);
    }

    let sup = sup.clone();
    thread::Builder::new()
        .name("penguin-burner-rtd3".to_string())
        .spawn(move || watch_loop(&sup, &probe, desktop_autostart_attempted))
        .expect("spawn rtd3 watcher thread");
}

fn runtime_pci_hint(sup: &Mutex<Supervisor>, probe: &Rtd3Probe) -> Option<String> {
    let hints: Vec<String> = supervisor::runtime_pci_bus_id_hints(sup)
        .into_iter()
        .filter_map(|hint| super::detect::sysfs_pci_addr(&hint))
        .collect();
    hints
        .iter()
        .find(|hint| probe.is_nvidia_gpu(hint))
        .cloned()
        .or_else(|| hints.into_iter().next())
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
    mut desktop_autostart_attempted: bool,
) {
    let mut tick_state = TickState::default();
    let mut clients = ClientDetector::system();

    loop {
        // The watcher is the only thread that parks runtimes, starts deferred
        // ones, and sweeps idle RPC backends; a single panicking tick must not
        // silently disable all three for the daemon's remaining lifetime.
        // Shared state stays usable across an unwind: the gate and supervisor
        // mutexes recover from poisoning, and TickState/ClientDetector hold
        // only counters and latches that the next tick re-derives.
        let tick = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            watch_tick(
                sup,
                probe,
                &mut desktop_autostart_attempted,
                &mut tick_state,
                &mut clients,
            )
        }));
        let delay = tick.unwrap_or_else(|_panic| {
            logging::error("deep sleep: watcher tick panicked; keeping the watcher alive");
            MOBILE_TICK
        });
        thread::sleep(delay);
    }
}

/// One watcher iteration; returns how long to sleep before the next.
fn watch_tick(
    sup: &Arc<Mutex<Supervisor>>,
    probe: &Rtd3Probe,
    desktop_autostart_attempted: &mut bool,
    tick_state: &mut TickState,
    clients: &mut ClientDetector,
) -> Duration {
    let previous_target = super::target_pci_addr();
    let hint = runtime_pci_hint(sup, probe);
    super::evaluate(probe, hint.as_deref());
    if super::target_pci_addr() != previous_target {
        // Client latches and countdowns are meaningful only for the card they
        // observed. Re-arm desktop startup too, for a persisted target change
        // made while no engine owns the profile slot.
        clients.reset();
        *tick_state = TickState::default();
        *desktop_autostart_attempted = false;
    }

    if super::protects_deep_sleep() {
        let released = gpu_rpc::release_idle_backends(RPC_BACKEND_IDLE_TTL);
        if released > 0 {
            logging::info(&format!(
                "deep sleep: released {released} idle GPU backend(s) so the GPU can suspend"
            ));
        }
        mobile_tick(
            sup,
            tick_state,
            clients,
            probe.nvidia_gpu_addrs().len() == 1,
        );
        return MOBILE_TICK;
    }

    clients.reset();
    super::set_autostart_deferred(false);
    if !*desktop_autostart_attempted && super::allows_desktop_autostart() {
        // Detection resolved from Unknown to Desktop after boot: make the
        // one eager-start attempt Desktop mode would have received at boot.
        *desktop_autostart_attempted = true;
        supervisor::start_autostart_if_configured(sup);
    }
    DESKTOP_TICK
}

/// One Mobile/Unknown observation tick: hold the deferred runtime back until
/// the GPU is genuinely in use, park an attached-but-idle runtime so the GPU
/// can suspend, and never fight another GPU owner.
fn mobile_tick(
    sup: &Arc<Mutex<Supervisor>>,
    state: &mut TickState,
    clients: &mut ClientDetector,
    legacy_target_unambiguous: bool,
) {
    // A scan/verification child owns the GPU exclusively; its own
    // `/dev/nvidia*` fds and activity must never satisfy the start policy —
    // starting the engine here would race the child's raw GPU writes.
    if supervisor::active_child_kind(sup).is_some() {
        clients.reset();
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
            match client_verdict(
                clients,
                ClientPhase::Attached,
                None,
                &|| supervisor::profile_gpu_index(sup),
            ) {
                ClientVerdict::Busy(observation) => {
                    super::record_gpu_clients(observation);
                    if state.idle_ticks > 0 {
                        logging::info(&format!(
                            "deep sleep: park countdown reset after {}s: a GPU client appeared",
                            state.idle_ticks
                        ));
                    }
                    state.end_countdown();
                }
                ClientVerdict::Idle(observation) => {
                    super::record_gpu_clients(observation);
                    advance_park_countdown(sup, state, legacy_target_unambiguous);
                }
                ClientVerdict::Pending => {
                    state.abandon_countdown("a safe GPU-client observation is pending");
                }
            }
        } else {
            state.abandon_countdown("the runtime engine is no longer running");
        }
        return;
    }
    state.abandon_countdown("the runtime is no longer attached");

    let pending = wakeable_runtime_pending(legacy_target_unambiguous);
    super::set_autostart_deferred(pending);
    if !pending {
        clients.reset();
        state.reset_wake_episode();
        return;
    }

    if super::last_runtime_status() == Some(RuntimePmStatus::Active) {
        state.active_ticks += 1;
    } else {
        if matches!(
            super::last_runtime_status(),
            Some(RuntimePmStatus::Suspended | RuntimePmStatus::Resuming)
        ) {
            clients.reset();
        }
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
        let target = super::target_pci_addr();
        // The index resolver opens NVML, so it must stay lazy: evaluated only
        // when the detector actually probes, never on latched ticks — a
        // per-tick NVML session would reset the driver's autosuspend timer
        // and keep the very GPU this feature parks from ever suspending.
        match client_verdict(
            clients,
            ClientPhase::Deferred,
            target.as_deref(),
            &|| {
                supervisor::deferred_runtime_gpu_index(
                    target.as_deref(),
                    legacy_target_unambiguous,
                )
            },
        ) {
            ClientVerdict::Busy(observation) => {
                super::record_gpu_clients(observation);
                logging::info(
                    "deep sleep: GPU is in use; starting the deferred persisted runtime",
                );
                if supervisor::start_autostart_if_configured(sup) {
                    state.start_attempted = true;
                    super::set_autostart_deferred(false);
                    super::set_parked(false);
                }
            }
            ClientVerdict::Idle(observation) => {
                super::record_gpu_clients(observation);
                if state.woke_from_suspend && !state.transient_wake_logged {
                    // Once per genuine wake episode: the GPU came out of
                    // suspend but nothing holds a counted client, so the
                    // runtime stays deferred.
                    state.transient_wake_logged = true;
                    logging::info(
                        "deep sleep: GPU awake but no counted clients; keeping the runtime deferred (transient wake?)",
                    );
                }
            }
            ClientVerdict::Pending => {}
        }
    }
}

fn client_verdict(
    detector: &mut ClientDetector,
    phase: ClientPhase,
    target_key: Option<&str>,
    resolve_gpu_index: &dyn Fn() -> Option<u32>,
) -> ClientVerdict {
    detector.tick(ClientTick {
        phase,
        target_key,
        resolve_gpu_index,
        fine_grained: super::fine_grained_mode(),
        runtime_status: super::last_runtime_status(),
    })
}

fn advance_park_countdown(
    sup: &Arc<Mutex<Supervisor>>,
    state: &mut TickState,
    legacy_target_unambiguous: bool,
) {
    state.idle_ticks += 1;
    if state.idle_ticks == 1 && !state.countdown_announced {
        state.countdown_announced = true;
        logging::info(&format!(
            "deep sleep: no GPU clients; parking the runtime in {}s unless one appears",
            park_after_idle_ticks() - state.idle_ticks
        ));
    }
    if state.idle_ticks < park_after_idle_ticks() {
        return;
    }

    state.idle_ticks = 0;
    logging::info("deep sleep: no GPU clients for the idle window; parking the runtime");
    if supervisor::park_runtime_for_deep_sleep(sup) {
        state.end_countdown();
        // Deferral first: a visible parked runtime must already say it will
        // reapply at the next real use.
        super::set_autostart_deferred(wakeable_runtime_pending(legacy_target_unambiguous));
        super::set_parked(true);
    } else if !state.park_refusal_logged {
        state.park_refusal_logged = true;
        logging::info(
            "deep sleep: park refused (a game session owns the runtime or the engine did not stop); retrying every idle window",
        );
    }
}

/// Persisted intent is worth waking for when the active-session runtime is
/// non-stock, or when any entry in a pending multi-GPU boot replay is
/// non-stock. The latter matters when the selected card itself is stock but
/// another card still needs its saved settings applied.
fn wakeable_runtime_pending(legacy_target_unambiguous: bool) -> bool {
    let target = super::target_pci_addr();
    supervisor::deferred_runtime_wakeable(target.as_deref(), legacy_target_unambiguous)
}
