//! Streaming-child management for the Auto-UV scan and profile verification:
//! spawn the (unchanged, Python) CLI child, relay its merged stdout/stderr as
//! JSON-line frames, and drive the stop protocol + detached kill ladder. Scan
//! behavior is parity with `stream_auto_uv_scan` (spec 01 §4.4); verification
//! reuses the same machinery with its own argv whitelist and stop marker.

use std::env;
use std::fs::File;
use std::io::{BufRead, BufReader, Read};
use std::os::unix::io::FromRawFd;
use std::os::unix::net::UnixStream;
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use serde_json::Value;

use crate::api::{write_json_line, StreamError, StreamFinished, StreamLine, StreamStarted};
use crate::argvspec;
use crate::paths;
use crate::supervisor::{self, ChildJob, ChildKind, ChildStart, Supervisor};

/// Kill-ladder timings. Shrunk by the undocumented `PENGUIN_BURNERD_TEST_TIMINGS`
/// env (integration tests only) so the ladder can be exercised quickly.
struct Timings {
    kill_after: Duration,
    term_grace: Duration,
    kill_grace: Duration,
}

fn timings() -> Timings {
    if env::var_os("PENGUIN_BURNERD_TEST_TIMINGS").is_some_and(|v| !v.is_empty()) {
        Timings {
            kill_after: Duration::from_secs(1),
            term_grace: Duration::from_millis(500),
            kill_grace: Duration::from_millis(500),
        }
    } else {
        Timings {
            kill_after: Duration::from_secs(30),
            term_grace: Duration::from_secs(5),
            kill_grace: Duration::from_secs(5),
        }
    }
}

/// The launch-failure label for each child kind.
fn launch_label(kind: ChildKind) -> &'static str {
    match kind {
        ChildKind::Scan => "Auto-UV scan",
        ChildKind::Verify => "profile verification",
    }
}

/// Handle a `start_auto_uv_scan` request on `writer` (the connection stream).
pub fn run_scan(sup: &Arc<Mutex<Supervisor>>, options: &Value, writer: &mut UnixStream) {
    let option_args = match argvspec::auto_uv_option_args(options) {
        Ok(args) => args,
        Err(err) => {
            write_json_line(writer, &StreamError::new(err));
            return;
        }
    };
    let mut argv = vec![
        "--auto-uv-voltage-scan".to_string(),
        "--json-events".to_string(),
        "--auto-uv-require-final-choice".to_string(),
    ];
    argv.extend(option_args);
    run_child(sup, ChildKind::Scan, argv, writer);
}

/// Handle a `start_profile_verification` request on `writer`. The argv mirrors
/// `ui/commands.py::profile_verify_command` (`--stability-test` prefix, then
/// the whitelisted options in its exact order); the stop-request file is owned
/// by the daemon and always appended, pointing at the effective user's config
/// dir like the Qt `VerifyController` did.
pub fn run_verification(sup: &Arc<Mutex<Supervisor>>, options: &Value, writer: &mut UnixStream) {
    let option_args = match argvspec::profile_verify_option_args(options) {
        Ok(args) => args,
        Err(err) => {
            write_json_line(writer, &StreamError::new(err));
            return;
        }
    };
    let mut argv = vec!["--stability-test".to_string()];
    argv.extend(option_args);
    argv.push("--stability-stop-request-file".to_string());
    argv.push(
        paths::profile_verify_stop_request_path()
            .display()
            .to_string(),
    );
    run_child(sup, ChildKind::Verify, argv, writer);
}

/// Start + stream one child. This consumes the connection: it streams frames
/// until the child finishes or the client disconnects, then returns.
fn run_child(
    sup: &Arc<Mutex<Supervisor>>,
    kind: ChildKind,
    argv: Vec<String>,
    writer: &mut UnixStream,
) {
    let program_file = supervisor::daemon_program_file();

    // The whole check-clear-stop-spawn-install runs atomically under the lock.
    let (job, reader) =
        match supervisor::begin_child(sup, kind, argv, |argv| spawn_child(&program_file, argv)) {
            ChildStart::Started(job, reader) => (job, reader),
            ChildStart::Refused(err) => {
                write_json_line(writer, &StreamError::new(err));
                return;
            }
            ChildStart::ClearFailed(err) => {
                // The scan text is byte-exact with the Python daemon ("Auto-UV",
                // not "Auto-UV scan"); the verification text is new.
                let label = match kind {
                    ChildKind::Scan => "Auto-UV",
                    ChildKind::Verify => "profile verification",
                };
                write_json_line(
                    writer,
                    &StreamError::new(format!("failed to clear stale {label} stop request: {err}")),
                );
                return;
            }
            ChildStart::SpawnFailed(err) => {
                write_json_line(
                    writer,
                    &StreamError::new(format!("failed to launch {}: {err}", launch_label(kind))),
                );
                return;
            }
        };

    // If the client is already gone by the time we announce "started", treat it
    // as a mid-stream disconnect (abort + monitor) rather than leaking the child.
    if !write_json_line(writer, &StreamStarted::new(job.proc.pid())) {
        disconnect_cleanup(sup, job, reader);
        return;
    }

    stream_stdout(sup, job, reader, writer);
}

/// Read the child's merged stdout, relaying each line as a `line` frame. On EOF,
/// emit `finished`; on client disconnect, run the abort + detached kill ladder.
fn stream_stdout(
    sup: &Arc<Mutex<Supervisor>>,
    job: Arc<ChildJob>,
    reader: File,
    writer: &mut UnixStream,
) {
    let mut reader = BufReader::new(reader);
    let mut client_gone = false;
    let mut buffer = Vec::new();
    loop {
        buffer.clear();
        match reader.read_until(b'\n', &mut buffer) {
            Ok(0) => break, // EOF
            Ok(_) => {
                // decode utf-8 with replacement (== Python errors="replace"),
                // keeping the trailing '\n' exactly as Python does.
                let line = String::from_utf8_lossy(&buffer).into_owned();
                if !write_json_line(writer, &StreamLine::new(line)) {
                    client_gone = true;
                    break;
                }
            }
            Err(_) => break, // read error → treat as EOF
        }
    }

    let mut completed = false;
    if !client_gone {
        let exit_code = job.proc.wait();
        completed = true;
        // A write failure here does not change `completed` (parity): finished has
        // already been decided.
        write_json_line(writer, &StreamFinished::new(exit_code));
    }

    // finally: `completed or poll() is not None` → finish; else disconnect.
    if completed || job.proc.poll().is_some() {
        drop(reader);
        supervisor::finish_child(sup, &job);
    } else {
        disconnect_cleanup(sup, job, reader.into_inner());
    }
}

/// Client-disconnect cleanup: stop-request marker + SIGINT, then detach a drain
/// thread and a kill-ladder monitor thread (parity `_start_detached_scan_monitor`).
fn disconnect_cleanup(sup: &Arc<Mutex<Supervisor>>, job: Arc<ChildJob>, reader: File) {
    // Disconnect is an abort (scan: abort-final-choice reason).
    job.kind.write_stop_request(true);
    job.proc.signal(libc::SIGINT);
    start_detached_monitor(sup, job, reader);
}

fn start_detached_monitor(sup: &Arc<Mutex<Supervisor>>, job: Arc<ChildJob>, reader: File) {
    // Drain thread: consume stdout to EOF so the child never blocks on a full pipe.
    let _ = thread::Builder::new()
        .name("penguin-burner-child-drain".to_string())
        .spawn(move || {
            let mut reader = reader;
            let mut sink = [0u8; 8192];
            while matches!(reader.read(&mut sink), Ok(n) if n > 0) {}
        });

    // Monitor thread: the kill ladder, then finish.
    let sup = sup.clone();
    let _ = thread::Builder::new()
        .name("penguin-burner-child-monitor".to_string())
        .spawn(move || {
            wait_for_detached_child(&sup, &job);
        });
}

fn wait_for_detached_child(sup: &Arc<Mutex<Supervisor>>, job: &Arc<ChildJob>) {
    let timings = timings();
    if job.proc.wait_timeout(timings.kill_after).is_none() {
        job.proc.signal(libc::SIGTERM);
        if job.proc.wait_timeout(timings.term_grace).is_none() {
            job.proc.signal(libc::SIGKILL);
            let _ = job.proc.wait_timeout(timings.kill_grace);
        }
    }
    supervisor::finish_child(sup, job);
}

/// Spawn the child with stdout and stderr merged onto one pipe, `cwd="/"`.
/// Returns the child and the read end of the pipe.
fn spawn_child(program_file: &str, argv: &[String]) -> std::io::Result<(Child, File)> {
    let mut fds = [0i32; 2];
    // SAFETY: pipe2 fills `fds` with a (read, write) pair; both are O_CLOEXEC so
    // they never leak into the child except via the explicit stdio dup below.
    let rc = unsafe { libc::pipe2(fds.as_mut_ptr(), libc::O_CLOEXEC) };
    if rc != 0 {
        return Err(std::io::Error::last_os_error());
    }
    let read_fd = fds[0];
    let write_fd = fds[1];

    // Own both ends immediately so they are always closed on any early return.
    // SAFETY: we exclusively own these freshly-created fds.
    let reader = unsafe { File::from_raw_fd(read_fd) };
    // SAFETY: same.
    let write_file = unsafe { File::from_raw_fd(write_fd) };
    let write_clone = write_file.try_clone()?;

    let mut command = Command::new("python3");
    command
        .arg(program_file)
        .args(argv)
        .current_dir("/")
        .stdout(Stdio::from(write_file))
        .stderr(Stdio::from(write_clone));
    // stdin is inherited (parity: the Python daemon does not redirect it).

    // The daemon blocks SIGINT/SIGTERM process-wide (signal thread) and the
    // mask survives exec — without a reset the child would never receive the
    // stop SIGINT (the Python daemon's children inherited a default mask).
    // SAFETY: runs between fork and exec; sigprocmask is async-signal-safe.
    unsafe {
        use std::os::unix::process::CommandExt;
        command.pre_exec(|| {
            let mut set: libc::sigset_t = std::mem::zeroed();
            libc::sigemptyset(&mut set);
            libc::sigprocmask(libc::SIG_SETMASK, &set, std::ptr::null_mut());
            Ok(())
        });
    }

    let child = command.spawn()?;
    // The parent's copies of the write end were moved into `command` and are now
    // closed; only the child holds it, so `reader` sees EOF when the child exits.
    Ok((child, reader))
}
