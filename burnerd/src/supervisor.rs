//! The single supervisor: one `Mutex<Supervisor>` holding the profile-engine job,
//! the streaming child job (Auto-UV scan or profile verification — one slot, so
//! they are mutually exclusive), and a generation counter (the Python identity
//! guard).
//!
//! Free functions take `&Mutex<Supervisor>` / `&Arc<Mutex<Supervisor>>` and manage
//! locking themselves so lock scopes stay tiny (parity with `_ACTIVE_SCAN_LOCK`).

use std::env;
use std::fs;
use std::fs::File;
use std::path::PathBuf;
use std::process::{Child, ExitStatus};
use std::sync::{Arc, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use serde::Serialize;
use serde_json::Value;

use crate::api::{ActiveJob, StartResult, StatusResult, StopResult};
use crate::logging;
use crate::paths;
use crate::profile::{self, EngineHandle, EngineOptions, StopOutcome};

const PROGRAM_FILE_ENV: &str = "PENGUIN_BURNER_DAEMON_PROGRAM_FILE";
/// Test-only override for the last-runtime state file (production uses the fixed
/// path below). Set by the integration tests to avoid touching `/var/lib`.
const STATE_FILE_ENV: &str = "PENGUIN_BURNERD_TEST_STATE_FILE";
const LAST_RUNTIME_STATE_PATH: &str = "/var/lib/penguin-burner/last-runtime.json";
const ENGINE_STOP_TIMEOUT: Duration = Duration::from_secs(10);

/// A spawned child process with a cached return code, mirroring `Popen.poll()`'s
/// caching so status can still report the exit code after the child is reaped.
pub struct ChildProc {
    child: Mutex<Child>,
    pid: u32,
    returncode: Mutex<Option<i32>>,
}

impl ChildProc {
    pub fn new(child: Child) -> Self {
        let pid = child.id();
        ChildProc {
            child: Mutex::new(child),
            pid,
            returncode: Mutex::new(None),
        }
    }

    pub fn pid(&self) -> u32 {
        self.pid
    }

    /// Non-blocking `poll()`: `None` if still running, else the (cached) code.
    pub fn poll(&self) -> Option<i32> {
        // Always lock child first, then returncode, to keep a single lock order.
        let mut child = self.child.lock().unwrap_or_else(|p| p.into_inner());
        let mut returncode = self.returncode.lock().unwrap_or_else(|p| p.into_inner());
        if let Some(code) = *returncode {
            return Some(code);
        }
        match child.try_wait() {
            Ok(Some(status)) => {
                let code = exit_code(status);
                *returncode = Some(code);
                Some(code)
            }
            _ => None,
        }
    }

    /// Block until the child exits and return its code.
    pub fn wait(&self) -> i32 {
        loop {
            if let Some(code) = self.poll() {
                return code;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    }

    /// Wait up to `timeout`; `None` on timeout, else the exit code.
    pub fn wait_timeout(&self, timeout: Duration) -> Option<i32> {
        let deadline = Instant::now() + timeout;
        loop {
            if let Some(code) = self.poll() {
                return Some(code);
            }
            if Instant::now() >= deadline {
                return None;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    }

    /// Send a signal, guarded like `Popen.send_signal` so a reaped (possibly
    /// reused) pid is never signalled. Errors are ignored (parity).
    pub fn signal(&self, sig: i32) {
        if self.poll().is_some() {
            return;
        }
        // SAFETY: kill() on our own child pid; a bad pid just returns an error.
        unsafe {
            libc::kill(self.pid as libc::pid_t, sig);
        }
    }
}

fn exit_code(status: ExitStatus) -> i32 {
    use std::os::unix::process::ExitStatusExt;
    // Python's poll() returns -signum when the child is killed by a signal.
    status
        .code()
        .unwrap_or_else(|| -status.signal().unwrap_or(0))
}

/// Which streaming child occupies the (single) child-job slot. A scan and a
/// profile verification are mutually exclusive: both own the GPU.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ChildKind {
    Scan,
    Verify,
}

impl ChildKind {
    /// Write this kind's cooperative stop marker. `abort_final_choice` selects
    /// the scan's abort-vs-offer reason (the verification marker has no reason).
    pub fn write_stop_request(self, abort_final_choice: bool) {
        match self {
            ChildKind::Scan => paths::write_auto_uv_stop_request(abort_final_choice),
            ChildKind::Verify => paths::write_profile_verify_stop_request(),
        }
    }

    /// Clear this kind's stale stop marker (`NotFound` is success).
    fn clear_stop_request(self) -> std::io::Result<()> {
        match self {
            ChildKind::Scan => paths::clear_auto_uv_stop_request(),
            ChildKind::Verify => paths::clear_profile_verify_stop_request(),
        }
    }
}

/// The active streaming child (Auto-UV scan or profile verification).
/// `generation` is the identity guard: a stale monitor only clears/restarts if
/// the supervisor still holds this exact job.
pub struct ChildJob {
    pub proc: ChildProc,
    pub argv: Vec<String>,
    pub generation: u64,
    pub kind: ChildKind,
}

struct ProfileJob {
    engine: EngineHandle,
    argv: Vec<String>,
}

pub struct Supervisor {
    profile: Option<ProfileJob>,
    child: Option<Arc<ChildJob>>,
    child_generation: u64,
    stop_timeout: Duration,
}

impl Default for Supervisor {
    fn default() -> Self {
        Self::new()
    }
}

impl Supervisor {
    pub fn new() -> Self {
        Supervisor {
            profile: None,
            child: None,
            child_generation: 0,
            stop_timeout: ENGINE_STOP_TIMEOUT,
        }
    }

    /// Test-only constructor with a short engine-stop timeout so the
    /// wedged-engine (`StopOutcome::TimedOut`) paths run in milliseconds.
    #[cfg(test)]
    fn with_stop_timeout(stop_timeout: Duration) -> Self {
        Supervisor {
            stop_timeout,
            ..Self::new()
        }
    }

    fn child_running_kind(&self) -> Option<ChildKind> {
        self.child
            .as_ref()
            .filter(|job| job.proc.poll().is_none())
            .map(|job| job.kind)
    }

    fn next_child_generation(&mut self) -> u64 {
        self.child_generation += 1;
        self.child_generation
    }

    /// Stop the engine so the child can own the GPU. Mirrors
    /// `_stop_autostart_runtime_for_scan`.
    ///
    /// A `TimedOut` stop means the engine thread is wedged in a blocking GPU
    /// call and could revive at any moment, writing fans/mem/VF/locked clocks
    /// over whatever owns the GPU next — so the caller must NOT proceed. The
    /// wedged job is retained (stop flag already set) so status stays truthful
    /// and a later attempt re-checks it.
    ///
    /// An engine that has ALREADY exited (a clean stop, an error exit, or a
    /// previously-wedged thread that finally returned and honored the stop flag)
    /// is dead: free the slot so it cannot block the post-child autostart
    /// restart (`start_autostart_if_configured` early-returns while
    /// `profile.is_some()`).
    fn stop_engine_for_child(&mut self, what: &str) -> Result<(), String> {
        if let Some(job) = self.profile.as_mut() {
            if job.engine.is_running() {
                match job.engine.stop(self.stop_timeout) {
                    StopOutcome::Stopped => self.profile = None,
                    StopOutcome::TimedOut => {
                        let message = format!(
                            "runtime profile engine did not stop (wedged GPU call?); refusing to start {what}"
                        );
                        logging::error(&message);
                        return Err(message);
                    }
                }
            } else {
                self.profile = None;
            }
        }
        Ok(())
    }
}

/// The refusal error for starting `requested` while `running` occupies the
/// child slot. The scan-vs-scan text is byte-exact with the Python daemon; the
/// verification texts are new (the method is milestone-B additive).
fn child_refusal(running: ChildKind, requested: ChildKind) -> String {
    match (running, requested) {
        (ChildKind::Scan, ChildKind::Scan) => "Auto-UV scan is already running",
        (ChildKind::Verify, ChildKind::Verify) => "profile verification is already running",
        (ChildKind::Scan, ChildKind::Verify) => {
            "cannot start profile verification while Auto-UV scan is running"
        }
        (ChildKind::Verify, ChildKind::Scan) => {
            "cannot start an Auto-UV scan while profile verification is running"
        }
    }
    .to_string()
}

fn guard(sup: &Mutex<Supervisor>) -> MutexGuard<'_, Supervisor> {
    sup.lock().unwrap_or_else(|poison| poison.into_inner())
}

/// `PENGUIN_BURNER_DAEMON_PROGRAM_FILE` resolved absolute, else this binary's path.
pub fn daemon_program_file() -> String {
    if let Ok(value) = env::var(PROGRAM_FILE_ENV) {
        let value = value.trim();
        if !value.is_empty() {
            return fs::canonicalize(value)
                .map(|path| path.display().to_string())
                .unwrap_or_else(|_| value.to_string());
        }
    }
    env::current_exe()
        .map(|path| path.display().to_string())
        .unwrap_or_default()
}

fn state_file_path() -> PathBuf {
    if let Some(path) = env::var_os(STATE_FILE_ENV) {
        if !path.is_empty() {
            return PathBuf::from(path);
        }
    }
    PathBuf::from(LAST_RUNTIME_STATE_PATH)
}

/// Persist the last runtime action (best-effort). `program_file` is written for
/// downgrade compatibility with the Python daemon and ignored on read.
fn persist_last_runtime(argv: &[String]) {
    #[derive(Serialize)]
    struct State<'a> {
        argv: &'a [String],
        program_file: String,
    }
    let path = state_file_path();
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    let state = State {
        argv,
        program_file: daemon_program_file(),
    };
    if let Ok(text) = serde_json::to_string(&state) {
        let _ = fs::write(&path, text);
    }
}

/// Read the persisted last runtime argv (or `None` on any error/invalid file).
fn load_last_runtime() -> Option<Vec<String>> {
    let text = fs::read_to_string(state_file_path()).ok()?;
    let data: Value = serde_json::from_str(&text).ok()?;
    let object = data.as_object()?;
    let program_file = object
        .get("program_file")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if program_file.is_empty() {
        return None;
    }
    let array = object.get("argv")?.as_array()?;
    let mut argv = Vec::with_capacity(array.len());
    for item in array {
        argv.push(item.as_str()?.to_string());
    }
    Some(argv)
}

pub fn status(sup: &Mutex<Supervisor>) -> StatusResult {
    let supervisor = guard(sup);
    let version = env!("CARGO_PKG_VERSION").to_string();

    if let Some(job) = &supervisor.child {
        let returncode = job.proc.poll();
        let (running_state, stopped_state, job_type) = match job.kind {
            ChildKind::Scan => (
                "auto_uv_scan_running",
                "auto_uv_scan_stopped",
                "auto_uv_scan",
            ),
            ChildKind::Verify => (
                "profile_verification_running",
                "profile_verification_stopped",
                "profile_verification",
            ),
        };
        let state = if returncode.is_none() {
            running_state
        } else {
            stopped_state
        };
        return StatusResult {
            state: state.to_string(),
            active_job: Some(ActiveJob {
                job_type,
                argv: job.argv.clone(),
                pid: job.proc.pid(),
                returncode,
            }),
            version,
        };
    }

    if let Some(job) = &supervisor.profile {
        let returncode = job.engine.returncode();
        let state = if returncode.is_none() {
            "runtime_profile_running"
        } else {
            "runtime_profile_stopped"
        };
        return StatusResult {
            state: state.to_string(),
            active_job: Some(ActiveJob {
                job_type: "runtime_profile",
                argv: job.argv.clone(),
                // The engine is in-process, so the "job pid" is the daemon's pid.
                pid: std::process::id(),
                returncode,
            }),
            version,
        };
    }

    StatusResult {
        state: "idle".to_string(),
        active_job: None,
        version,
    }
}

pub fn stop_auto_uv_scan(sup: &Mutex<Supervisor>) -> StopResult {
    stop_child(sup, ChildKind::Scan)
}

pub fn stop_profile_verification(sup: &Mutex<Supervisor>) -> StopResult {
    stop_child(sup, ChildKind::Verify)
}

pub fn active_child_kind(sup: &Mutex<Supervisor>) -> Option<ChildKind> {
    guard(sup).child_running_kind()
}

/// Cooperative stop: write the kind's stop-request marker FIRST, then SIGINT
/// (ordered protocol, parity with the Python daemon's scan stop).
fn stop_child(sup: &Mutex<Supervisor>, kind: ChildKind) -> StopResult {
    let supervisor = guard(sup);
    match &supervisor.child {
        Some(job) if job.kind == kind && job.proc.poll().is_none() => {
            let pid = job.proc.pid();
            kind.write_stop_request(false);
            job.proc.signal(libc::SIGINT);
            StopResult::stopped(pid)
        }
        _ => StopResult::idle(),
    }
}

pub fn start_runtime_profile(
    sup: &Mutex<Supervisor>,
    argv: Vec<String>,
) -> Result<StartResult, String> {
    {
        let mut supervisor = guard(sup);
        match supervisor.child_running_kind() {
            Some(ChildKind::Scan) => {
                return Err(
                    "cannot start a runtime profile while Auto-UV scan is running".to_string(),
                );
            }
            Some(ChildKind::Verify) => {
                return Err(
                    "cannot start a runtime profile while profile verification is running"
                        .to_string(),
                );
            }
            None => {}
        }
        supervisor.stop_engine_for_child("a new runtime profile")?;
        let engine = profile::start(EngineOptions::from_argv(&argv)).map_err(|e| e.to_string())?;
        supervisor.profile = Some(ProfileJob {
            engine,
            argv: argv.clone(),
        });
    }
    // Persist outside the lock (best-effort), like the Python daemon.
    persist_last_runtime(&argv);
    Ok(StartResult {
        started: true,
        pid: std::process::id(),
        argv,
    })
}

pub fn stop_runtime_profile(sup: &Mutex<Supervisor>) -> Result<StopResult, String> {
    let mut supervisor = guard(sup);
    let stop_timeout = supervisor.stop_timeout;
    match supervisor.profile.as_mut() {
        Some(job) if job.engine.is_running() => {
            let pid = std::process::id();
            match job.engine.stop(stop_timeout) {
                StopOutcome::Stopped => {
                    supervisor.profile = None;
                    Ok(StopResult::stopped(pid))
                }
                // Wedged engine: keep the job (so future scan/verification/
                // profile starts still see it and refuse) and tell the client
                // the truth. The stop flag is set, so the engine exits — and
                // runs its cleanup — as soon as the wedged call returns.
                StopOutcome::TimedOut => {
                    let message =
                        "runtime profile engine did not stop within timeout (wedged GPU call?)"
                            .to_string();
                    logging::error(&message);
                    Err(message)
                }
            }
        }
        // None or already-exited: do NOT clear the file (parity), report idle.
        _ => Ok(StopResult::idle()),
    }
}

/// Result of the atomic child-start critical section.
pub enum ChildStart {
    Refused(String),
    ClearFailed(String),
    SpawnFailed(String),
    Started(Arc<ChildJob>, File),
}

/// Atomically (under one lock, like Python's `_ACTIVE_SCAN_LOCK`): refuse a
/// concurrent scan/verification, clear the kind's stale stop-request, stop the
/// engine, spawn the child via `spawn`, and install the job. Holding the lock
/// across the spawn is what prevents two racing requests from both launching.
pub fn begin_child(
    sup: &Mutex<Supervisor>,
    kind: ChildKind,
    argv: Vec<String>,
    spawn: impl FnOnce(&[String]) -> std::io::Result<(Child, File)>,
) -> ChildStart {
    let mut supervisor = guard(sup);
    if let Some(running) = supervisor.child_running_kind() {
        return ChildStart::Refused(child_refusal(running, kind));
    }
    if let Err(err) = kind.clear_stop_request() {
        return ChildStart::ClearFailed(err.to_string());
    }
    let what = match kind {
        ChildKind::Scan => "an Auto-UV scan",
        ChildKind::Verify => "profile verification",
    };
    if let Err(err) = supervisor.stop_engine_for_child(what) {
        return ChildStart::Refused(err);
    }
    let (child, reader) = match spawn(&argv) {
        Ok(pair) => pair,
        Err(err) => return ChildStart::SpawnFailed(err.to_string()),
    };
    let generation = supervisor.next_child_generation();
    let job = Arc::new(ChildJob {
        proc: ChildProc::new(child),
        argv,
        generation,
        kind,
    });
    supervisor.child = Some(job.clone());
    ChildStart::Started(job, reader)
}

/// Clear the child slot if it still holds `job` (identity guard) and, if so,
/// restart the persisted runtime profile now that the GPU is free.
pub fn finish_child(sup: &Arc<Mutex<Supervisor>>, job: &Arc<ChildJob>) {
    let restart = {
        let mut supervisor = guard(sup);
        match &supervisor.child {
            Some(current) if current.generation == job.generation => {
                supervisor.child = None;
                true
            }
            _ => false,
        }
    };
    if restart {
        start_autostart_if_configured(sup);
    }
}

/// Start the engine from the persisted last-runtime state, if any. Idempotent:
/// does nothing when a profile job is already present (parity).
pub fn start_autostart_if_configured(sup: &Arc<Mutex<Supervisor>>) {
    let mut supervisor = guard(sup);
    if supervisor.profile.is_some() {
        return;
    }
    let Some(argv) = load_last_runtime() else {
        return;
    };
    match profile::start(EngineOptions::from_argv(&argv)) {
        Ok(engine) => {
            logging::info(&format!("autostart runtime profile: {}", argv.join(" ")));
            supervisor.profile = Some(ProfileJob { engine, argv });
        }
        Err(err) => logging::error(&format!("failed to start autostart runtime: {err}")),
    }
}

/// Shutdown cleanup: stop the in-process engine (A3 releases fans + clock lock).
pub fn shutdown(sup: &Mutex<Supervisor>) {
    let mut supervisor = guard(sup);
    let stop_timeout = supervisor.stop_timeout;
    if let Some(mut job) = supervisor.profile.take() {
        if job.engine.stop(stop_timeout) == StopOutcome::TimedOut {
            // The process exits anyway; the wedged thread cannot run its
            // cleanup, so at least say so in the journal.
            logging::error(
                "engine thread did not stop during shutdown; exiting without its fan/clock cleanup",
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // `STATE_FILE_ENV` is process-global; serialize the tests that mutate it.
    static STATE_ENV_LOCK: Mutex<()> = Mutex::new(());

    struct StateEnvGuard {
        previous: Option<std::ffi::OsString>,
    }
    impl StateEnvGuard {
        fn new(path: &std::path::Path) -> Self {
            let previous = env::var_os(STATE_FILE_ENV);
            env::set_var(STATE_FILE_ENV, path);
            StateEnvGuard { previous }
        }
    }
    impl Drop for StateEnvGuard {
        fn drop(&mut self) {
            match &self.previous {
                Some(value) => env::set_var(STATE_FILE_ENV, value),
                None => env::remove_var(STATE_FILE_ENV),
            }
        }
    }

    // --- wedged-engine (StopOutcome::TimedOut) refusal paths ------------------

    use std::sync::atomic::{AtomicBool, Ordering};

    /// A supervisor holding a running engine that ignores its stop flag for
    /// `wedge` (longer than the supervisor's `stop_timeout`).
    fn wedged_supervisor(wedge: Duration, stop_timeout: Duration) -> Mutex<Supervisor> {
        let sup = Mutex::new(Supervisor::with_stop_timeout(stop_timeout));
        guard(&sup).profile = Some(ProfileJob {
            engine: profile::wedged_engine_for_test(wedge),
            argv: vec!["--auto-uv-profile".to_string(), "test".to_string()],
        });
        sup
    }

    #[test]
    fn begin_child_refuses_scan_when_engine_stop_times_out() {
        let sup = wedged_supervisor(Duration::from_secs(5), Duration::from_millis(50));
        let spawned = AtomicBool::new(false);
        let result = begin_child(&sup, ChildKind::Scan, vec![], |_| {
            spawned.store(true, Ordering::SeqCst);
            Err(std::io::Error::other("must not spawn"))
        });
        match result {
            ChildStart::Refused(err) => {
                assert!(err.contains("did not stop"), "{err}");
                assert!(err.contains("Auto-UV scan"), "{err}");
            }
            _ => panic!("expected Refused"),
        }
        assert!(
            !spawned.load(Ordering::SeqCst),
            "the child must not be spawned over a wedged engine"
        );
        assert!(
            guard(&sup).profile.is_some(),
            "the wedged job must be retained so later attempts re-check it"
        );
    }

    #[test]
    fn begin_child_refuses_verification_when_engine_stop_times_out() {
        let sup = wedged_supervisor(Duration::from_secs(5), Duration::from_millis(50));
        let result = begin_child(&sup, ChildKind::Verify, vec![], |_| {
            Err(std::io::Error::other("must not spawn"))
        });
        match result {
            ChildStart::Refused(err) => {
                assert!(err.contains("did not stop"), "{err}");
                assert!(err.contains("profile verification"), "{err}");
            }
            _ => panic!("expected Refused"),
        }
    }

    #[test]
    fn start_runtime_profile_refuses_when_engine_stop_times_out() {
        let sup = wedged_supervisor(Duration::from_secs(5), Duration::from_millis(50));
        let err = start_runtime_profile(
            &sup,
            vec!["--auto-uv-profile".to_string(), "other".to_string()],
        )
        .unwrap_err();
        assert!(err.contains("did not stop"), "{err}");
        assert!(guard(&sup).profile.is_some());
    }

    #[test]
    fn stop_runtime_profile_errors_when_engine_wedged() {
        let sup = wedged_supervisor(Duration::from_secs(5), Duration::from_millis(50));
        let err = stop_runtime_profile(&sup).unwrap_err();
        assert!(err.contains("did not stop"), "{err}");
        assert!(
            guard(&sup).profile.is_some(),
            "the wedged job must be retained (dropping it would let a scan start \
             while the engine can still write to the GPU)"
        );
    }

    #[test]
    fn begin_child_recovers_after_wedged_engine_exits() {
        let sup = wedged_supervisor(Duration::from_millis(300), Duration::from_millis(50));
        let result = begin_child(&sup, ChildKind::Scan, vec![], |_| {
            Err(std::io::Error::other("no spawn"))
        });
        assert!(matches!(result, ChildStart::Refused(_)));
        // The wedged engine eventually exits on its own; a retry must get past
        // the engine gate (reaching the spawn closure proves it).
        let deadline = Instant::now() + Duration::from_secs(3);
        loop {
            std::thread::sleep(Duration::from_millis(50));
            let result = begin_child(&sup, ChildKind::Scan, vec![], |_| {
                Err(std::io::Error::other("no spawn"))
            });
            match result {
                ChildStart::SpawnFailed(_) => break,
                ChildStart::Refused(_) if Instant::now() < deadline => continue,
                ChildStart::Refused(err) => panic!("still refused after engine exit: {err}"),
                _ => panic!("expected SpawnFailed or Refused"),
            }
        }
    }

    /// After a wedged engine finally exits, the dead job must NOT linger in the
    /// slot — otherwise `start_autostart_if_configured` (early-returns while
    /// `profile.is_some()`) never re-applies the persisted profile after a scan.
    /// `stop_engine_for_child` frees the exited slot on the next start.
    #[test]
    fn stop_engine_for_child_frees_an_already_exited_job() {
        let sup = wedged_supervisor(Duration::from_millis(100), Duration::from_millis(50));
        // First attempt times out and retains the (still-wedged) job.
        assert!(guard(&sup).stop_engine_for_child("a scan").is_err());
        // Let the engine thread finish.
        std::thread::sleep(Duration::from_millis(300));
        assert!(!guard(&sup).profile.as_ref().unwrap().engine.is_running());
        // The next start sees a dead engine and frees the slot (Ok, not Err).
        assert!(guard(&sup).stop_engine_for_child("a scan").is_ok());
        assert!(
            guard(&sup).profile.is_none(),
            "an exited engine's slot must be freed so autostart can restart"
        );
    }

    /// A retry against a STILL-wedged engine must return quickly rather than
    /// re-paying the full stop timeout under the supervisor mutex (which would
    /// starve the watchdog and hang status). The second stop polls once.
    #[test]
    fn repeated_stop_of_wedged_engine_is_fast() {
        let sup = wedged_supervisor(Duration::from_secs(5), Duration::from_millis(400));
        // First stop pays the timeout.
        assert!(guard(&sup).stop_engine_for_child("a scan").is_err());
        // Second stop, still wedged: must be near-instant, not another 400ms.
        let start = Instant::now();
        assert!(guard(&sup).stop_engine_for_child("a scan").is_err());
        assert!(
            start.elapsed() < Duration::from_millis(200),
            "a retry against a still-wedged engine must not re-wait the timeout: {:?}",
            start.elapsed()
        );
    }

    #[test]
    fn last_runtime_round_trip() {
        let _lock = STATE_ENV_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let dir = env::temp_dir().join(format!("pb-state-a-{}", std::process::id()));
        let _ = fs::create_dir_all(&dir);
        let path = dir.join("last-runtime.json");
        let _guard = StateEnvGuard::new(&path);

        let argv = vec![
            "--auto-uv-profile".to_string(),
            "profile-a".to_string(),
            "--silent-fan-curve".to_string(),
        ];
        persist_last_runtime(&argv);
        assert_eq!(load_last_runtime(), Some(argv));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn last_runtime_missing_program_file_is_none() {
        let _lock = STATE_ENV_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let dir = env::temp_dir().join(format!("pb-state-b-{}", std::process::id()));
        let _ = fs::create_dir_all(&dir);
        let path = dir.join("last-runtime.json");
        let _guard = StateEnvGuard::new(&path);

        fs::write(&path, r#"{"argv":["--gpu-index","0"]}"#).unwrap();
        assert_eq!(load_last_runtime(), None);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn last_runtime_malformed_is_none() {
        let _lock = STATE_ENV_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let dir = env::temp_dir().join(format!("pb-state-c-{}", std::process::id()));
        let _ = fs::create_dir_all(&dir);
        let path = dir.join("last-runtime.json");
        let _guard = StateEnvGuard::new(&path);

        fs::write(&path, "{not json").unwrap();
        assert_eq!(load_last_runtime(), None);

        fs::write(&path, r#"{"argv":[1,2],"program_file":"/x"}"#).unwrap();
        assert_eq!(load_last_runtime(), None);
        let _ = fs::remove_dir_all(&dir);
    }
}
