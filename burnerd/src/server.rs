//! JSON-lines socket server: bind + chmod the socket, accept one thread per
//! connection, gate on SO_PEERCRED, and route requests to `api`/`scan`.

use std::env;
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::{FileTypeExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::thread;

use serde_json::Value;

use crate::api::{self, StreamError};
use crate::scan;
use crate::supervisor::Supervisor;

const ALLOWED_UID_ENV: &str = "PENGUIN_BURNER_DAEMON_ALLOWED_UID";
const UID_DENIED_LINE: &[u8] = b"{\"ok\":false,\"error\":\"daemon client uid is not allowed\"}\n";

/// Bind and prepare the listening socket. Errors carry the exact Python message
/// for the "path exists and is not a socket" case.
pub fn bind(path: &Path) -> anyhow::Result<UnixListener> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    if path.exists() {
        let metadata = fs::metadata(path)?;
        if !metadata.file_type().is_socket() {
            anyhow::bail!(
                "daemon socket path exists and is not a socket: {}",
                path.display()
            );
        }
        fs::remove_file(path)?;
    }
    let listener = UnixListener::bind(path)?;
    // World rw: access is gated by SO_PEERCRED, not file perms.
    fs::set_permissions(path, fs::Permissions::from_mode(0o666))?;
    Ok(listener)
}

/// Accept loop. Each connection is handled on its own thread; a panic in a
/// connection is caught so it never takes the daemon down.
pub fn serve(listener: UnixListener, sup: Arc<Mutex<Supervisor>>) {
    for incoming in listener.incoming() {
        let stream = match incoming {
            Ok(stream) => stream,
            Err(err) => {
                crate::logging::warn(&format!("accept failed: {err}"));
                continue; // transient accept error
            }
        };
        let sup = sup.clone();
        let _ = thread::Builder::new()
            .name("penguin-burner-conn".to_string())
            .spawn(move || {
                let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    handle_connection(stream, &sup);
                }));
            });
    }
}

fn handle_connection(stream: UnixStream, sup: &Arc<Mutex<Supervisor>>) {
    if !peer_uid_allowed(&stream) {
        let mut writer = &stream;
        let _ = writer.write_all(UID_DENIED_LINE);
        let _ = writer.flush();
        return;
    }

    let reader = match stream.try_clone() {
        Ok(clone) => BufReader::new(clone),
        Err(_) => return,
    };
    let mut reader = reader;
    let mut writer = stream;
    let mut buffer = Vec::new();

    loop {
        buffer.clear();
        match reader.read_until(b'\n', &mut buffer) {
            Ok(0) => return, // EOF
            Ok(_) => {}
            Err(_) => return,
        }
        let decoded = String::from_utf8_lossy(&buffer);
        let line = decoded.trim();
        if line.is_empty() {
            continue;
        }
        match serde_json::from_str::<Value>(line) {
            Ok(value) if is_start_scan(&value) => {
                handle_start_scan(&value, sup, &mut writer);
                return; // streaming consumes the connection
            }
            Ok(value) => {
                let response = api::handle_request(sup, &value);
                if !api::write_response(&mut writer, response) {
                    return;
                }
            }
            Err(err) => {
                // Malformed JSON: same envelope shape as Python; the parser's
                // message text differs (serde_json vs Python json) — see STATUS.
                if !api::write_response(&mut writer, Err(err.to_string())) {
                    return;
                }
            }
        }
    }
}

fn is_start_scan(value: &Value) -> bool {
    value.get("method").and_then(Value::as_str) == Some("start_auto_uv_scan")
}

fn handle_start_scan(value: &Value, sup: &Arc<Mutex<Supervisor>>, writer: &mut UnixStream) {
    // `is_start_scan` already established this is an object with a string method.
    let object = value.as_object().expect("start_auto_uv_scan is an object");
    let mut unknown: Vec<&str> = object
        .keys()
        .filter(|key| key.as_str() != "method" && key.as_str() != "options")
        .map(String::as_str)
        .collect();
    unknown.sort_unstable();
    if !unknown.is_empty() {
        api::write_json_line(
            writer,
            &StreamError::new(format!("unknown request field: {}", unknown.join(", "))),
        );
        return;
    }
    let options = object.get("options").cloned().unwrap_or(Value::Null);
    scan::run_scan(sup, &options, writer);
}

fn peer_uid_allowed(stream: &UnixStream) -> bool {
    let allowed = env::var(ALLOWED_UID_ENV).unwrap_or_default();
    let allowed = allowed.trim();
    if allowed.is_empty() {
        return true;
    }
    match peer_uid(stream) {
        Some(uid) => uid == 0 || uid.to_string() == allowed,
        None => false,
    }
}

/// Read the connecting peer's uid via SO_PEERCRED (mirrors the Python daemon's
/// `struct.unpack("3i", ...)` on the same option).
fn peer_uid(stream: &UnixStream) -> Option<u32> {
    use std::os::unix::io::AsRawFd;
    // SAFETY: an all-zero ucred is a valid initial value; it is filled below.
    let mut cred: libc::ucred = unsafe { std::mem::zeroed() };
    let mut len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    // SAFETY: getsockopt writes a ucred of `len` bytes into `cred`.
    let rc = unsafe {
        libc::getsockopt(
            stream.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            &mut cred as *mut libc::ucred as *mut libc::c_void,
            &mut len,
        )
    };
    if rc != 0 {
        return None;
    }
    Some(cred.uid as u32)
}
