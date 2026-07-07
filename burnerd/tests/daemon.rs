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
            .env("PENGUIN_BURNERD_TEST_INERT_ENGINE", "1")
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

    fn verify_stop_request_file(&self) -> PathBuf {
        self.home
            .join(".config")
            .join("PenguinBurner")
            .join("profile-verify-stop-requested")
    }

    fn profiles_dir(&self) -> PathBuf {
        self.home
            .join(".config")
            .join("PenguinBurner")
            .join("auto-uv-profiles")
    }

    /// Send a streaming start request, assert the `started` frame, and return
    /// the connection reader plus the child pid.
    fn start_streaming(&self, request: &str) -> (UnixStream, BufReader<UnixStream>, u32) {
        let mut stream = self.connect();
        stream.write_all(request.as_bytes()).unwrap();
        stream.write_all(b"\n").unwrap();
        stream.flush().unwrap();
        let mut reader = BufReader::new(stream.try_clone().unwrap());
        let started: Value = serde_json::from_str(&read_line(&mut reader).unwrap()).unwrap();
        assert_eq!(started["control"], "started", "frame: {started}");
        let pid = started["pid"].as_u64().unwrap() as u32;
        (stream, reader, pid)
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
    let scan_pid = started["pid"].as_u64().unwrap() as u32;

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

    // The SIGINT actually reaches the child (the child's signal mask is reset
    // at spawn; the daemon itself blocks SIGINT process-wide).
    wait_until(|| !pid_alive(scan_pid), Duration::from_secs(5));
    assert!(!pid_alive(scan_pid), "scan child did not receive SIGINT");
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

// --- milestone B: GPU-write RPCs (mock-GPU seam) -------------------------------

const MOCK_GPU: (&str, &str) = ("PENGUIN_BURNERD_TEST_MOCK_GPU", "1");

#[test]
fn gpu_writes_run_against_the_mock_and_echo_ops() {
    let daemon = Daemon::start(&[MOCK_GPU]);

    let response = daemon.request(
        r#"{"method":"gpu_apply_vf_offsets","gpu_index":0,"offsets":[[12,150000],[13,-15000]]}"#,
    );
    assert_eq!(response["ok"], Value::Bool(true));
    assert_eq!(response["result"]["applied"], 2);
    assert_eq!(
        response["result"]["mock_ops"],
        serde_json::json!(["ApplyVfOffsets { offsets: [(12, 150000), (13, -15000)] }"])
    );

    let response =
        daemon.request(r#"{"method":"gpu_apply_power_limit","gpu_index":0,"power_limit_w":300}"#);
    assert_eq!(response["result"]["applied_w"], 300);
    assert_eq!(
        response["result"]["mock_ops"],
        serde_json::json!(["ApplyPowerLimit { power_limit_w: 300 }"])
    );

    let response = daemon.request(
        r#"{"method":"gpu_apply_clock_offsets","gpu_index":0,"gpc_clk_vf_offset_mhz":30,"mem_clk_vf_offset_mhz":1500}"#,
    );
    assert_eq!(response["result"]["gpc_clk_vf_offset_mhz"], 30);
    assert_eq!(response["result"]["gpc_clk_vf_offset_readback_mhz"], 30);
    assert_eq!(response["result"]["mem_clk_vf_offset_mhz"], 1500);
    assert_eq!(response["result"]["mem_clk_vf_offset_readback_mhz"], 1500);

    // The mock's canned steps are 1800/1900/2000/2100: 1950 floors to 1900.
    let response = daemon
        .request(r#"{"method":"gpu_apply_locked_core_clock","gpu_index":0,"clock_mhz":1950}"#);
    assert_eq!(response["result"]["requested_clock_mhz"], 1950);
    assert_eq!(response["result"]["applied_clock_mhz"], 1900);
    assert_eq!(response["result"]["mode"], "floor");
    assert_eq!(
        response["result"]["mock_ops"],
        serde_json::json!([
            "ApplyLockedCoreClock { clock_mhz: 1950, prefer_not_above: true, snap: true }"
        ])
    );

    let response = daemon.request(
        r#"{"method":"gpu_apply_locked_core_clock_range","gpu_index":0,"min_mhz":1850,"max_mhz":2150}"#,
    );
    assert_eq!(response["result"]["applied_min_clock_mhz"], 1900);
    assert_eq!(response["result"]["min_mode"], "ceil");
    assert_eq!(response["result"]["applied_max_clock_mhz"], 2100);
    assert_eq!(response["result"]["max_mode"], "floor");

    for (method, key) in [
        ("gpu_reset_locked_core_clocks", "reset"),
        ("gpu_reset_locked_memory_clocks", "reset"),
        ("gpu_enable_persistence_mode", "enabled"),
    ] {
        let response = daemon.request(&format!(r#"{{"method":"{method}","gpu_index":0}}"#));
        assert_eq!(response["ok"], Value::Bool(true), "{method}");
        assert_eq!(response["result"][key], Value::Bool(true), "{method}");
        assert_eq!(
            response["result"]["mock_ops"].as_array().unwrap().len(),
            1,
            "{method}"
        );
    }
}

#[test]
fn gpu_write_validation_errors_over_the_wire() {
    let daemon = Daemon::start(&[MOCK_GPU]);

    let cases = [
        (
            r#"{"method":"gpu_apply_power_limit","gpu_index":0,"power_limit_w":300,"bogus":1}"#,
            "unknown request field: bogus",
        ),
        (
            r#"{"method":"gpu_apply_power_limit","power_limit_w":300}"#,
            "gpu_index must be an integer",
        ),
        (
            r#"{"method":"gpu_apply_power_limit","gpu_index":-1,"power_limit_w":300}"#,
            "gpu_index must be an integer",
        ),
        (
            r#"{"method":"gpu_apply_power_limit","gpu_index":0,"power_limit_w":"300"}"#,
            "power_limit_w must be an integer",
        ),
        (
            r#"{"method":"gpu_apply_vf_offsets","gpu_index":0,"offsets":[[1,2,3]]}"#,
            "offsets must be a list of [index, offset_khz] integer pairs",
        ),
        (
            r#"{"method":"gpu_apply_locked_core_clock","gpu_index":0,"clock_mhz":1950,"prefer_not_above":"yes"}"#,
            "prefer_not_above must be a boolean",
        ),
        // Unknown-field rejection precedes method dispatch (existing precedence),
        // so the not-a-method probe carries no fields.
        (
            r#"{"method":"gpu_frobnicate"}"#,
            "unknown daemon method: gpu_frobnicate",
        ),
    ];
    for (request, expected) in cases {
        let response = daemon.request(request);
        assert_eq!(response["ok"], Value::Bool(false), "request: {request}");
        assert_eq!(response["error"], expected, "request: {request}");
    }
}

#[test]
fn gpu_write_backend_error_text_is_relayed() {
    let daemon = Daemon::start(&[
        MOCK_GPU,
        ("PENGUIN_BURNERD_TEST_MOCK_GPU_FAIL", "apply_power_limit_w"),
    ]);
    let response =
        daemon.request(r#"{"method":"gpu_apply_power_limit","gpu_index":0,"power_limit_w":300}"#);
    assert_eq!(response["ok"], Value::Bool(false));
    assert_eq!(response["error"], "apply_power_limit_w mock failure");

    // Other methods on the same backend still work.
    let response = daemon.request(r#"{"method":"gpu_reset_locked_core_clocks","gpu_index":0}"#);
    assert_eq!(response["ok"], Value::Bool(true));
}

#[test]
fn gpu_writes_are_legal_while_a_scan_runs() {
    let daemon = Daemon::start(&[MOCK_GPU]);
    let (_stream, _reader, _pid) =
        daemon.start_streaming(r#"{"method":"start_auto_uv_scan","options":{"gpu_index":0}}"#);

    // The scan child is the intended caller of the write RPCs: no arbitration.
    let response =
        daemon.request(r#"{"method":"gpu_apply_power_limit","gpu_index":0,"power_limit_w":250}"#);
    assert_eq!(response["ok"], Value::Bool(true));
    assert_eq!(response["result"]["applied_w"], 250);
}

// --- milestone B: profile verification (streaming) -----------------------------

#[test]
fn verification_streams_frames_and_builds_the_exact_argv() {
    let argv_file = std::env::temp_dir().join(format!("pb-vargv-{}.json", std::process::id()));
    let _ = std::fs::remove_file(&argv_file);
    let daemon = Daemon::start(&[
        ("SCAN_STUB_EXIT_AFTER_LINES", "1"),
        ("SCAN_STUB_ARGV_FILE", argv_file.to_str().unwrap()),
    ]);

    let mut stream = daemon.connect();
    stream
        .write_all(br#"{"method":"start_profile_verification","options":{"gpu_index":0,"stability_seconds":120,"auto_uv_profile":"profile-a"}}"#)
        .unwrap();
    stream.write_all(b"\n").unwrap();
    stream.flush().unwrap();

    let mut reader = BufReader::new(stream);
    let mut frames = Vec::new();
    while let Some(line) = read_line(&mut reader) {
        frames.push(serde_json::from_str::<Value>(&line).unwrap());
    }
    assert_eq!(frames[0]["control"], "started");
    assert_eq!(frames[1]["line"], "{\"event\":\"auto_uv_start\"}\n");
    assert_eq!(frames[2]["line"], "human line\n");
    assert_eq!(frames[3]["control"], "finished");
    assert_eq!(frames[3]["exit_code"], 0);

    // The exact argv of `ui/commands.py::profile_verify_command`, with the
    // daemon-owned stop-request file appended last.
    let argv: Value = serde_json::from_str(&std::fs::read_to_string(&argv_file).unwrap()).unwrap();
    assert_eq!(
        argv,
        serde_json::json!([
            "--stability-test",
            "--stability-seconds",
            "120",
            "--gpu-index",
            "0",
            "--auto-uv-profile",
            "profile-a",
            "--stability-stop-request-file",
            daemon.verify_stop_request_file().to_str().unwrap(),
        ])
    );
    let _ = std::fs::remove_file(&argv_file);
}

#[test]
fn unknown_verification_option_is_rejected() {
    let daemon = Daemon::start(&[]);
    let mut stream = daemon.connect();
    stream
        .write_all(br#"{"method":"start_profile_verification","options":{"bogus":1}}"#)
        .unwrap();
    stream.write_all(b"\n").unwrap();
    stream.flush().unwrap();
    let mut reader = BufReader::new(stream);
    let line = read_line(&mut reader).unwrap();
    assert_eq!(
        line,
        "{\"ok\":false,\"error\":\"unknown profile verification option: bogus\"}\n"
    );
}

#[test]
fn stop_profile_verification_writes_marker_and_ends_child() {
    let daemon = Daemon::start(&[]);
    let (_stream, mut reader, pid) = daemon
        .start_streaming(r#"{"method":"start_profile_verification","options":{"gpu_index":0}}"#);

    let status = daemon.request(r#"{"method":"status"}"#);
    assert_eq!(status["result"]["state"], "profile_verification_running");
    assert_eq!(
        status["result"]["active_job"]["type"],
        "profile_verification"
    );

    let stop = daemon.request(r#"{"method":"stop_profile_verification"}"#);
    assert_eq!(stop["ok"], Value::Bool(true));
    assert_eq!(stop["result"]["stopped"], Value::Bool(true));
    assert_eq!(stop["result"]["pid"].as_u64().unwrap() as u32, pid);

    let marker = std::fs::read_to_string(daemon.verify_stop_request_file()).unwrap();
    assert_eq!(marker, "stop requested by PenguinBurner daemon client\n");

    // The child exits on SIGINT and the stream finishes.
    let mut finished = None;
    while let Some(line) = read_line(&mut reader) {
        let frame: Value = serde_json::from_str(&line).unwrap();
        if frame["control"] == "finished" {
            finished = Some(frame);
            break;
        }
    }
    assert!(finished.is_some(), "no finished frame after stop");
    wait_until(|| !pid_alive(pid), Duration::from_secs(5));
    let status = daemon.request(r#"{"method":"status"}"#);
    assert_eq!(status["result"]["state"], "idle");

    // Stopping again reports idle (no verification running).
    let stop = daemon.request(r#"{"method":"stop_profile_verification"}"#);
    assert_eq!(stop["result"]["stopped"], Value::Bool(false));
    assert_eq!(stop["result"]["state"], "idle");
}

#[test]
fn stop_auto_uv_scan_does_not_stop_a_verification() {
    let daemon = Daemon::start(&[]);
    let (_stream, _reader, pid) = daemon
        .start_streaming(r#"{"method":"start_profile_verification","options":{"gpu_index":0}}"#);

    let stop = daemon.request(r#"{"method":"stop_auto_uv_scan"}"#);
    assert_eq!(stop["result"]["stopped"], Value::Bool(false));
    assert!(pid_alive(pid), "verification child was signalled");
}

#[test]
fn verification_disconnect_writes_marker_and_ends_child() {
    let daemon = Daemon::start(&[]);
    let (stream, reader, pid) = daemon
        .start_streaming(r#"{"method":"start_profile_verification","options":{"gpu_index":0}}"#);

    drop(reader);
    drop(stream);

    let path = daemon.verify_stop_request_file();
    wait_until(|| path.exists(), Duration::from_secs(5));
    assert_eq!(
        std::fs::read_to_string(&path).unwrap(),
        "stop requested by PenguinBurner daemon client\n"
    );
    wait_until(|| !pid_alive(pid), Duration::from_secs(8));
    assert!(!pid_alive(pid), "verification child survived disconnect");
    let status = daemon.request(r#"{"method":"status"}"#);
    assert_eq!(status["result"]["state"], "idle");
}

#[test]
fn scan_and_verification_refuse_each_other() {
    let daemon = Daemon::start(&[]);

    // Scan running → verification, second scan, and runtime profile refused.
    let (_stream, _reader, _pid) =
        daemon.start_streaming(r#"{"method":"start_auto_uv_scan","options":{"gpu_index":0}}"#);
    let refused =
        daemon.request(r#"{"method":"start_profile_verification","options":{"gpu_index":0}}"#);
    assert_eq!(
        refused["error"],
        "cannot start profile verification while Auto-UV scan is running"
    );
    let refused = daemon.request(r#"{"method":"start_runtime_profile","argv":[]}"#);
    assert_eq!(
        refused["error"],
        "cannot start a runtime profile while Auto-UV scan is running"
    );
}

#[test]
fn verification_refuses_scan_second_verification_and_profile() {
    let daemon = Daemon::start(&[]);
    let (_stream, _reader, _pid) = daemon
        .start_streaming(r#"{"method":"start_profile_verification","options":{"gpu_index":0}}"#);

    let refused = daemon.request(r#"{"method":"start_auto_uv_scan","options":{"gpu_index":0}}"#);
    assert_eq!(
        refused["error"],
        "cannot start an Auto-UV scan while profile verification is running"
    );
    let refused =
        daemon.request(r#"{"method":"start_profile_verification","options":{"gpu_index":0}}"#);
    assert_eq!(refused["error"], "profile verification is already running");
    let refused = daemon.request(r#"{"method":"start_runtime_profile","argv":[]}"#);
    assert_eq!(
        refused["error"],
        "cannot start a runtime profile while profile verification is running"
    );
}

// --- milestone B: profile deletion ---------------------------------------------

#[test]
fn delete_auto_uv_profiles_happy_path_and_attacks() {
    let daemon = Daemon::start(&[]);
    let profiles_dir = daemon.profiles_dir();
    std::fs::create_dir_all(&profiles_dir).unwrap();
    std::fs::write(profiles_dir.join("auto-uv-profile-a.json"), "{}").unwrap();
    std::fs::write(profiles_dir.join("auto-uv-profile-b.json"), "{}").unwrap();
    let victim = daemon.home.join("victim.json");
    std::fs::write(&victim, "precious").unwrap();

    // Attack: ../ traversal out of the profiles dir.
    let attack = profiles_dir.join("../../../victim.json");
    let response = daemon.request(&format!(
        r#"{{"method":"delete_auto_uv_profiles","paths":["{}"]}}"#,
        attack.display()
    ));
    assert_eq!(response["ok"], Value::Bool(false));
    assert!(
        response["error"]
            .as_str()
            .unwrap()
            .starts_with("refusing to delete a path outside"),
        "{response}"
    );
    assert!(victim.exists());

    // Attack: absolute path outside the dir.
    let response = daemon.request(&format!(
        r#"{{"method":"delete_auto_uv_profiles","paths":["{}"]}}"#,
        victim.display()
    ));
    assert_eq!(response["ok"], Value::Bool(false));
    assert!(victim.exists());

    // Attack: symlink inside the dir escaping it.
    let link = profiles_dir.join("evil.json");
    std::os::unix::fs::symlink(&victim, &link).unwrap();
    let response = daemon.request(&format!(
        r#"{{"method":"delete_auto_uv_profiles","paths":["{}"]}}"#,
        link.display()
    ));
    assert_eq!(response["ok"], Value::Bool(false));
    assert!(victim.exists());
    assert!(std::fs::symlink_metadata(&link).is_ok());

    // Validation: paths must be a string list; unknown fields rejected.
    let response = daemon.request(r#"{"method":"delete_auto_uv_profiles"}"#);
    assert_eq!(
        response["error"],
        "profile paths must be a JSON string list"
    );
    let response = daemon.request(r#"{"method":"delete_auto_uv_profiles","paths":[],"x":1}"#);
    assert_eq!(response["error"], "unknown request field: x");

    // A failing batch deletes nothing, even when it contains a valid path.
    let response = daemon.request(&format!(
        r#"{{"method":"delete_auto_uv_profiles","paths":["{}","{}"]}}"#,
        profiles_dir.join("auto-uv-profile-a.json").display(),
        victim.display()
    ));
    assert_eq!(response["ok"], Value::Bool(false));
    assert!(profiles_dir.join("auto-uv-profile-a.json").exists());

    // Happy path: both profiles deleted, canonical paths reported.
    let response = daemon.request(&format!(
        r#"{{"method":"delete_auto_uv_profiles","paths":["{}","{}"]}}"#,
        profiles_dir.join("auto-uv-profile-a.json").display(),
        profiles_dir.join("auto-uv-profile-b.json").display()
    ));
    assert_eq!(response["ok"], Value::Bool(true), "{response}");
    let deleted = response["result"]["deleted"].as_array().unwrap();
    assert_eq!(deleted.len(), 2);
    assert!(deleted[0]
        .as_str()
        .unwrap()
        .ends_with("auto-uv-profile-a.json"));
    assert!(!profiles_dir.join("auto-uv-profile-a.json").exists());
    assert!(!profiles_dir.join("auto-uv-profile-b.json").exists());

    // Empty list is a successful no-op.
    let response = daemon.request(r#"{"method":"delete_auto_uv_profiles","paths":[]}"#);
    assert_eq!(response["result"]["deleted"], serde_json::json!([]));
}
