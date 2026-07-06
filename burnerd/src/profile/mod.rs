//! Profile engine facade.
//!
//! STUB: the real NVML/NVAPI engine (apply VF curve, fan loop, adaptive tiers,
//! telemetry) arrives in wave A3. This module keeps only the *surface* the
//! supervisor talks to so A1 can manage the engine's lifecycle exactly like the
//! Python daemon managed its runtime-profile child process.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

/// Whitelisted runtime-profile argv parsed into the knobs the engine needs.
#[derive(Debug, Clone, Default)]
pub struct EngineOptions {
    pub tier: String,
    pub silent_fan_curve: bool,
    pub adaptive: bool,
    pub gpu_index: Option<u32>,
}

impl EngineOptions {
    /// Parse an already-validated runtime argv (see `argvspec::parse_runtime_argv`).
    pub fn from_argv(argv: &[String]) -> Self {
        let mut options = EngineOptions::default();
        let mut index = 0;
        while index < argv.len() {
            let arg = &argv[index];
            if arg == "--auto-uv-profile" {
                if let Some(value) = argv.get(index + 1) {
                    options.tier = value.clone();
                }
                index += 2;
                continue;
            }
            if let Some(value) = arg.strip_prefix("--auto-uv-profile=") {
                options.tier = value.to_string();
                index += 1;
                continue;
            }
            if arg == "--gpu-index" {
                if let Some(value) = argv.get(index + 1) {
                    options.gpu_index = value.parse().ok();
                }
                index += 2;
                continue;
            }
            if let Some(value) = arg.strip_prefix("--gpu-index=") {
                options.gpu_index = value.parse().ok();
                index += 1;
                continue;
            }
            if arg == "--silent-fan-curve" {
                options.silent_fan_curve = true;
            } else if arg == "--adaptive-auto-uv" {
                options.adaptive = true;
            }
            index += 1;
        }
        options
    }
}

/// Outcome of `EngineHandle::stop`.
#[derive(Debug, PartialEq, Eq)]
pub enum StopOutcome {
    Stopped,
    /// The engine thread did not exit within the timeout (wedged). A3 escalates
    /// this to a loud log + `exit(1)`; A1's stub never produces it.
    TimedOut,
}

/// A running engine. `returncode` mirrors the Python child's `poll()`: `None`
/// while running, `Some(0)` after a clean stop, `Some(1)` after an engine error.
pub struct EngineHandle {
    stop_flag: Arc<AtomicBool>,
    returncode: Arc<Mutex<Option<i32>>>,
    thread: Option<JoinHandle<()>>,
}

impl EngineHandle {
    pub fn returncode(&self) -> Option<i32> {
        *self
            .returncode
            .lock()
            .unwrap_or_else(|poison| poison.into_inner())
    }

    pub fn is_running(&self) -> bool {
        self.returncode().is_none()
    }

    /// Signal the engine to stop and join it, waiting at most `timeout`.
    pub fn stop(&mut self, timeout: Duration) -> StopOutcome {
        self.stop_flag.store(true, Ordering::SeqCst);
        let Some(thread) = self.thread.take() else {
            return StopOutcome::Stopped;
        };
        let deadline = Instant::now() + timeout;
        while !thread.is_finished() {
            if Instant::now() >= deadline {
                // Detach the wedged thread; A3 will log loudly + exit(1) here.
                return StopOutcome::TimedOut;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        let _ = thread.join();
        let mut returncode = self
            .returncode
            .lock()
            .unwrap_or_else(|poison| poison.into_inner());
        if returncode.is_none() {
            *returncode = Some(0);
        }
        StopOutcome::Stopped
    }
}

/// Start the engine. STUB: idles until asked to stop, then reports a clean exit.
/// A3 replaces the thread body with the real apply + fan/adaptive/telemetry loop.
pub fn start(options: EngineOptions) -> anyhow::Result<EngineHandle> {
    let stop_flag = Arc::new(AtomicBool::new(false));
    let returncode = Arc::new(Mutex::new(None));
    let thread_flag = stop_flag.clone();
    let thread_returncode = returncode.clone();
    let thread = std::thread::Builder::new()
        .name("penguin-burnerd-engine".to_string())
        .spawn(move || {
            // STUB: replaced in wave A3. Options are captured but unused for now.
            let _ = &options;
            while !thread_flag.load(Ordering::SeqCst) {
                std::thread::sleep(Duration::from_millis(50));
            }
            let mut code = thread_returncode
                .lock()
                .unwrap_or_else(|poison| poison.into_inner());
            if code.is_none() {
                *code = Some(0);
            }
        })?;
    Ok(EngineHandle {
        stop_flag,
        returncode,
        thread: Some(thread),
    })
}
