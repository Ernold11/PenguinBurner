//! Integration tests: build+spawn the real binary on a temp socket, drive it with
//! raw JSON-line requests, and assert wire-level parity with the Python daemon.
//!
//! A stub `python3` script stands in for the Auto-UV scan child. Its behavior is
//! selected by env the daemon inherits and forwards to the child.

use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU32, Ordering};
use std::time::{Duration, Instant};

use serde_json::Value;

static COUNTER: AtomicU32 = AtomicU32::new(0);

const STUB: &str = r#"import os, sys, signal, time

argv_file = os.environ.get("SCAN_STUB_ARGV_FILE")
if argv_file:
    with open(argv_file, "w") as handle:
        import json
        handle.write(json.dumps(sys.argv[1:]))

sys.stdout.write('{"event":"auto_uv_start"}\n')
sys.stdout.write('human line\n')
sys.stdout.flush()

if os.environ.get("SCAN_STUB_EXIT_AFTER_LINES") == "1":
    sys.exit(0)

if os.environ.get("SCAN_STUB_IGNORE_SIGINT") == "1":
    signal.signal(signal.SIGINT, signal.SIG_IGN)
else:
    signal.signal(signal.SIGINT, lambda *_: os._exit(0))

initial_ppid = os.getppid()
while True:
    # Exit if the daemon (our parent) died and we were reparented.
    if os.getppid() != initial_ppid:
        os._exit(0)
    time.sleep(0.05)
"#;

struct Daemon {
    child: Child,
    socket: PathBuf,
    home: PathBuf,
    state_file: PathBuf,
    dir: PathBuf,
}

impl Daemon {
    fn start(extra_env: &[(&str, &str)]) -> Daemon {
        let n = COUNTER.fetch_add(1, Ordering::SeqCst);
        let dir = std::env::temp_dir().join(format!("pb-int-{}-{}", std::process::id(), n));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let home = dir.join("home");
        std::fs::create_dir_all(&home).unwrap();
        let socket = dir.join("sock");
        let state_file = dir.join("state.json");
        let stub = dir.join("scan_stub.py");
        std::fs::write(&stub, STUB).unwrap();

        let mut command = Command::new(env!("CARGO_BIN_EXE_penguin-burnerd"));
        command
            .arg("--socket")
            .arg(&socket)
            .env("PENGUIN_BURNER_DAEMON_PROGRAM_FILE", &stub)
            .env("PENGUIN_BURNER_HOME", &home)
            .env("PENGUIN_BURNERD_TEST_STATE_FILE", &state_file)
            .env("PENGUIN_BURNERD_TEST_TIMINGS", "1")
            .env_remove("PENGUIN_BURNER_DAEMON_ALLOWED_UID")
            .env_remove("NOTIFY_SOCKET")
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        for (key, value) in extra_env {
            command.env(key, value);
        }
        let child = command.spawn().expect("spawn daemon");

        let daemon = Daemon {
            child,
            socket,
            home,
            state_file,
            dir,
        };
        daemon.wait_for_socket();
        daemon
    }

    fn wait_for_socket(&self) {
        for _ in 0..200 {
            if self.socket.exists() {
                return;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        panic!("socket was not created: {}", self.socket.display());
    }

    fn connect(&self) -> UnixStream {
        let stream = UnixStream::connect(&self.socket).expect("connect");
        stream
            .set_read_timeout(Some(Duration::from_secs(10)))
            .unwrap();
        stream
    }

    /// Send one request, read exactly one response line, parse it.
    fn request(&self, request: &str) -> Value {
        let mut stream = self.connect();
        stream.write_all(request.as_bytes()).unwrap();
        stream.write_all(b"\n").unwrap();
        stream.flush().unwrap();
        let mut reader = BufReader::new(stream);
        let line = read_line(&mut reader).expect("a response line");
        serde_json::from_str(&line).expect("valid JSON response")
    }

    fn stop_request_file(&self) -> PathBuf {
        self.home
            .join(".config")
            .join("PenguinBurner")
            .join("auto-uv-stop-requested")
    }
}

impl Drop for Daemon {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        // Give reparented stub children a moment to notice and self-exit.
        std::thread::sleep(Duration::from_millis(150));
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

fn read_line<R: BufRead>(reader: &mut R) -> Option<String> {
    let mut line = String::new();
    match reader.read_line(&mut line) {
        Ok(0) => None,
        Ok(_) => Some(line),
        Err(_) => None,
    }
}

fn pid_alive(pid: u32) -> bool {
    // SAFETY: kill(pid, 0) only probes for existence.
    unsafe { libc::kill(pid as libc::pid_t, 0) == 0 }
}

// --- tests -------------------------------------------------------------------

#[test]
fn status_is_idle_when_nothing_running() {
    let daemon = Daemon::start(&[]);
    let response = daemon.request(r#"{"method":"status"}"#);
    assert_eq!(response["ok"], Value::Bool(true));
    let result = &response["result"];
    assert_eq!(result["state"], "idle");
    assert_eq!(result["active_job"], Value::Null);
    assert!(result["version"].is_string());
}

#[test]
fn scan_streams_started_lines_finished() {
    let argv_file = std::env::temp_dir().join(format!("pb-argv-{}.json", std::process::id()));
    let _ = std::fs::remove_file(&argv_file);
    let daemon = Daemon::start(&[
        ("SCAN_STUB_EXIT_AFTER_LINES", "1"),
        ("SCAN_STUB_ARGV_FILE", argv_file.to_str().unwrap()),
    ]);

    let mut stream = daemon.connect();
    stream
        .write_all(br#"{"method":"start_auto_uv_scan","options":{"gpu_index":0,"auto_uv_mode":"performance"}}"#)
        .unwrap();
    stream.write_all(b"\n").unwrap();
    stream.flush().unwrap();

    let mut reader = BufReader::new(stream);
    let mut frames = Vec::new();
    while let Some(line) = read_line(&mut reader) {
        frames.push(serde_json::from_str::<Value>(&line).unwrap());
    }

    assert_eq!(frames[0]["ok"], Value::Bool(true));
    assert_eq!(frames[0]["control"], "started");
    assert!(frames[0]["pid"].is_number());
    assert_eq!(frames[1]["line"], "{\"event\":\"auto_uv_start\"}\n");
    assert_eq!(frames[2]["line"], "human line\n");
    assert_eq!(frames[3]["control"], "finished");
    assert_eq!(frames[3]["exit_code"], 0);

    // The child was launched with the exact scan argv.
    let argv: Value = serde_json::from_str(&std::fs::read_to_string(&argv_file).unwrap()).unwrap();
    assert_eq!(
        argv,
        serde_json::json!([
            "--auto-uv-voltage-scan",
            "--json-events",
            "--auto-uv-require-final-choice",
            "--gpu-index",
            "0",
            "--auto-uv-mode",
            "performance"
        ])
    );
    let _ = std::fs::remove_file(&argv_file);
}

#[test]
fn second_scan_is_refused_while_one_runs() {
    let daemon = Daemon::start(&[]);

    // First scan keeps running (the default stub loops until signalled).
    let mut first = daemon.connect();
    first
        .write_all(br#"{"method":"start_auto_uv_scan","options":{"gpu_index":0}}"#)
        .unwrap();
    first.write_all(b"\n").unwrap();
    first.flush().unwrap();
    let mut first_reader = BufReader::new(first);
    let started = read_line(&mut first_reader).unwrap();
    assert!(started.contains("\"started\""));

    // Second scan on a fresh connection is refused.
    let second = daemon.request(r#"{"method":"start_auto_uv_scan","options":{"gpu_index":0}}"#);
    assert_eq!(second["ok"], Value::Bool(false));
    assert_eq!(second["error"], "Auto-UV scan is already running");
}

#[test]
fn stop_auto_uv_writes_offer_stop_file() {
    let daemon = Daemon::start(&[]);

    let mut stream = daemon.connect();
    stream
        .write_all(br#"{"method":"start_auto_uv_scan","options":{"gpu_index":0}}"#)
        .unwrap();
    stream.write_all(b"\n").unwrap();
    stream.flush().unwrap();
    let mut reader = BufReader::new(stream);
    let started: Value = serde_json::from_str(&read_line(&mut reader).unwrap()).unwrap();
    assert_eq!(started["control"], "started");

    let stop = daemon.request(r#"{"method":"stop_auto_uv_scan"}"#);
    assert_eq!(stop["ok"], Value::Bool(true));
    assert_eq!(stop["result"]["stopped"], Value::Bool(true));

    // The stop-request marker exists with the offer reason.
    let path = daemon.stop_request_file();
    let mut content = String::new();
    for _ in 0..100 {
        if let Ok(text) = std::fs::read_to_string(&path) {
            content = text;
            break;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    assert_eq!(
        content,
        "stop requested by PenguinBurner daemon client\nreason=offer-final-choice\n"
    );
}

#[test]
fn disconnect_writes_abort_stop_file_and_ends_scan() {
    let daemon = Daemon::start(&[]);

    let mut stream = daemon.connect();
    stream
        .write_all(br#"{"method":"start_auto_uv_scan","options":{"gpu_index":0}}"#)
        .unwrap();
    stream.write_all(b"\n").unwrap();
    stream.flush().unwrap();
    let mut reader = BufReader::new(stream.try_clone().unwrap());
    let started: Value = serde_json::from_str(&read_line(&mut reader).unwrap()).unwrap();
    assert_eq!(started["control"], "started");
    let scan_pid = started["pid"].as_u64().unwrap() as u32;

    // Disconnect mid-stream: drop both the reader clone and the stream.
    drop(reader);
    drop(stream);

    // The daemon writes an abort stop-request and SIGINTs the child (which exits).
    let path = daemon.stop_request_file();
    let mut content = String::new();
    for _ in 0..200 {
        if let Ok(text) = std::fs::read_to_string(&path) {
            content = text;
            break;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    assert_eq!(
        content,
        "stop requested by PenguinBurner daemon client\nreason=abort-final-choice\n"
    );

    // The scan child terminates and the daemon returns to idle.
    wait_until(|| !pid_alive(scan_pid), Duration::from_secs(5));
    let status = daemon.request(r#"{"method":"status"}"#);
    assert_eq!(status["result"]["state"], "idle");
}

#[test]
fn kill_ladder_terminates_a_sigint_ignoring_scan() {
    let daemon = Daemon::start(&[("SCAN_STUB_IGNORE_SIGINT", "1")]);

    let mut stream = daemon.connect();
    stream
        .write_all(br#"{"method":"start_auto_uv_scan","options":{"gpu_index":0}}"#)
        .unwrap();
    stream.write_all(b"\n").unwrap();
    stream.flush().unwrap();
    let mut reader = BufReader::new(stream.try_clone().unwrap());
    let started: Value = serde_json::from_str(&read_line(&mut reader).unwrap()).unwrap();
    let scan_pid = started["pid"].as_u64().unwrap() as u32;
    assert!(pid_alive(scan_pid));

    // Disconnect: SIGINT is ignored, so the ladder escalates to SIGTERM/SIGKILL.
    drop(reader);
    drop(stream);

    // Under PENGUIN_BURNERD_TEST_TIMINGS the ladder fires within ~2s.
    wait_until(|| !pid_alive(scan_pid), Duration::from_secs(8));
    assert!(
        !pid_alive(scan_pid),
        "kill ladder did not terminate the scan"
    );
}

#[test]
fn runtime_profile_start_stop_transitions_and_state_file() {
    let daemon = Daemon::start(&[]);
    let daemon_pid = daemon.child.id();

    let start = daemon.request(
        r#"{"method":"start_runtime_profile","argv":["--auto-uv-profile","profile-a","--silent-fan-curve"]}"#,
    );
    assert_eq!(start["ok"], Value::Bool(true));
    let result = &start["result"];
    assert_eq!(result["started"], Value::Bool(true));
    assert_eq!(result["pid"], daemon_pid);
    assert_eq!(
        result["argv"],
        serde_json::json!(["--auto-uv-profile", "profile-a", "--silent-fan-curve"])
    );

    let status = daemon.request(r#"{"method":"status"}"#);
    let status_result = &status["result"];
    assert_eq!(status_result["state"], "runtime_profile_running");
    let job = &status_result["active_job"];
    assert_eq!(job["type"], "runtime_profile");
    assert_eq!(job["pid"], daemon_pid);
    assert_eq!(job["returncode"], Value::Null);
    assert_eq!(
        job["argv"],
        serde_json::json!(["--auto-uv-profile", "profile-a", "--silent-fan-curve"])
    );

    // State file persisted with the argv.
    let state: Value =
        serde_json::from_str(&std::fs::read_to_string(&daemon.state_file).unwrap()).unwrap();
    assert_eq!(
        state["argv"],
        serde_json::json!(["--auto-uv-profile", "profile-a", "--silent-fan-curve"])
    );
    assert!(state["program_file"].is_string());

    // Stop → idle, but the state file is NOT cleared (parity).
    let stop = daemon.request(r#"{"method":"stop_runtime_profile"}"#);
    assert_eq!(stop["result"]["stopped"], Value::Bool(true));
    let status = daemon.request(r#"{"method":"status"}"#);
    assert_eq!(status["result"]["state"], "idle");
    assert!(daemon.state_file.exists());
}

#[test]
fn autostart_runs_the_persisted_runtime_profile() {
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("pb-seed-{}-{}", std::process::id(), n));
    std::fs::create_dir_all(&dir).unwrap();
    let state_file = dir.join("state.json");
    std::fs::write(
        &state_file,
        r#"{"argv":["--auto-uv-profile","seeded"],"program_file":"/does/not/matter"}"#,
    )
    .unwrap();

    let daemon = Daemon::start(&[(
        "PENGUIN_BURNERD_TEST_STATE_FILE",
        state_file.to_str().unwrap(),
    )]);
    let status = daemon.request(r#"{"method":"status"}"#);
    assert_eq!(status["result"]["state"], "runtime_profile_running");
    assert_eq!(
        status["result"]["active_job"]["argv"],
        serde_json::json!(["--auto-uv-profile", "seeded"])
    );
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn peercred_denies_non_matching_uid() {
    let daemon = Daemon::start(&[("PENGUIN_BURNER_DAEMON_ALLOWED_UID", "999999")]);
    let mut stream = daemon.connect();
    // The daemon writes the denial and closes before reading any request, so the
    // client just reads (writing to the closed socket would race into an RST).
    let mut text = String::new();
    stream.read_to_string(&mut text).unwrap();
    assert_eq!(
        text,
        "{\"ok\":false,\"error\":\"daemon client uid is not allowed\"}\n"
    );
}

#[test]
fn malformed_json_returns_error_envelope() {
    let daemon = Daemon::start(&[]);
    let mut stream = daemon.connect();
    stream.write_all(b"{bad json\n").unwrap();
    stream.flush().unwrap();
    let mut reader = BufReader::new(stream);
    let line = read_line(&mut reader).unwrap();
    let response: Value = serde_json::from_str(&line).unwrap();
    assert_eq!(response["ok"], Value::Bool(false));
    assert!(response["error"].is_string());
}

#[test]
fn unknown_method_and_field_error_lines_are_byte_exact() {
    let daemon = Daemon::start(&[]);

    let mut stream = daemon.connect();
    stream.write_all(br#"{"method":"run_cli"}"#).unwrap();
    stream.write_all(b"\n").unwrap();
    stream.flush().unwrap();
    let mut reader = BufReader::new(stream.try_clone().unwrap());
    let line = read_line(&mut reader).unwrap();
    assert_eq!(
        line,
        "{\"ok\":false,\"error\":\"unknown daemon method: run_cli\"}\n"
    );

    // Reuse the same connection: the non-streaming path loops.
    stream
        .write_all(br#"{"method":"status","argv":["--x"]}"#)
        .unwrap();
    stream.write_all(b"\n").unwrap();
    stream.flush().unwrap();
    let line = read_line(&mut reader).unwrap();
    assert_eq!(
        line,
        "{\"ok\":false,\"error\":\"unknown request field: argv\"}\n"
    );
}

#[test]
fn unknown_scan_option_is_rejected() {
    let daemon = Daemon::start(&[]);
    let mut stream = daemon.connect();
    stream
        .write_all(br#"{"method":"start_auto_uv_scan","options":{"bogus":1}}"#)
        .unwrap();
    stream.write_all(b"\n").unwrap();
    stream.flush().unwrap();
    let mut reader = BufReader::new(stream);
    let line = read_line(&mut reader).unwrap();
    assert_eq!(
        line,
        "{\"ok\":false,\"error\":\"unknown Auto-UV option: bogus\"}\n"
    );
}

fn wait_until<F: Fn() -> bool>(predicate: F, timeout: Duration) {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if predicate() {
            return;
        }
        std::thread::sleep(Duration::from_millis(25));
    }
}
