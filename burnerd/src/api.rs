//! Wire response shapes + non-streaming request dispatch (`handle_request`).
//!
//! Every struct here serializes with its fields in declaration order, so the
//! emitted JSON key order is byte-compatible with the Python daemon (whose dicts
//! preserve insertion order). serde_json's default compact form matches Python's
//! `separators=(",", ":")`.

use std::io::Write;
use std::sync::Mutex;

use serde::Serialize;
use serde_json::Value;

use crate::argvspec;
use crate::delete;
use crate::gpu_rpc;
use crate::supervisor::{self, Supervisor};

#[derive(Debug, Serialize)]
pub struct ActiveJob {
    #[serde(rename = "type")]
    pub job_type: &'static str,
    pub argv: Vec<String>,
    pub pid: u32,
    pub returncode: Option<i32>,
}

#[derive(Debug, Serialize)]
pub struct StatusResult {
    pub state: String,
    pub active_job: Option<ActiveJob>,
    pub version: String,
}

#[derive(Debug, Serialize)]
pub struct StartResult {
    pub started: bool,
    pub pid: u32,
    pub argv: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct StopResult {
    pub stopped: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub state: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pid: Option<u32>,
}

impl StopResult {
    pub fn idle() -> Self {
        StopResult {
            stopped: false,
            state: Some("idle"),
            pid: None,
        }
    }

    pub fn stopped(pid: u32) -> Self {
        StopResult {
            stopped: true,
            state: None,
            pid: Some(pid),
        }
    }
}

#[derive(Debug, Serialize)]
#[serde(untagged)]
pub enum MethodResult {
    Status(StatusResult),
    Start(StartResult),
    Stop(StopResult),
    /// Dynamic result dicts (milestone-B `gpu_*` writes + profile deletion).
    Value(Value),
}

#[derive(Debug, Serialize)]
struct OkEnvelope {
    ok: bool,
    result: MethodResult,
}

#[derive(Debug, Serialize)]
struct ErrEnvelope {
    ok: bool,
    error: String,
}

// Streaming payloads for `start_auto_uv_scan`.

#[derive(Debug, Serialize)]
pub struct StreamStarted {
    pub ok: bool,
    pub control: &'static str,
    pub pid: u32,
}

#[derive(Debug, Serialize)]
pub struct StreamLine {
    pub ok: bool,
    pub line: String,
}

#[derive(Debug, Serialize)]
pub struct StreamFinished {
    pub ok: bool,
    pub control: &'static str,
    pub exit_code: i32,
}

#[derive(Debug, Serialize)]
pub struct StreamError {
    pub ok: bool,
    pub error: String,
}

impl StreamStarted {
    pub fn new(pid: u32) -> Self {
        StreamStarted {
            ok: true,
            control: "started",
            pid,
        }
    }
}

impl StreamLine {
    pub fn new(line: String) -> Self {
        StreamLine { ok: true, line }
    }
}

impl StreamFinished {
    pub fn new(exit_code: i32) -> Self {
        StreamFinished {
            ok: true,
            control: "finished",
            exit_code,
        }
    }
}

impl StreamError {
    pub fn new(error: String) -> Self {
        StreamError { ok: false, error }
    }
}

/// Serialize `payload` as one compact JSON line and write it. Returns `false` if
/// the write/flush failed (client disconnected) — parity with the Python
/// handler's `_write_stream_payload` returning `False` on a broken pipe.
pub fn write_json_line<W: Write, T: Serialize>(writer: &mut W, payload: &T) -> bool {
    let Ok(mut text) = serde_json::to_string(payload) else {
        return false;
    };
    text.push('\n');
    writer
        .write_all(text.as_bytes())
        .and_then(|()| writer.flush())
        .is_ok()
}

/// Write a non-streaming response envelope. Returns `false` on write failure.
pub fn write_response<W: Write>(writer: &mut W, response: Result<MethodResult, String>) -> bool {
    match response {
        Ok(result) => write_json_line(writer, &OkEnvelope { ok: true, result }),
        Err(error) => write_json_line(writer, &ErrEnvelope { ok: false, error }),
    }
}

/// Dispatch a non-streaming request (`handle_request`). Field validation and the
/// error strings are byte-exact with `runtime/daemon_api.py::handle_request`;
/// the milestone-B methods (`gpu_*`, `stop_profile_verification`,
/// `delete_auto_uv_profiles`) are additive and reuse the same validation style.
///
/// Engine/scan interplay: `gpu_*` writes carry NO supervisor arbitration — they
/// are legal in any state. During a scan or verification the profile engine is
/// stopped by design and the streaming child is the intended caller of these
/// writes; adding arbitration here would deadlock the very flow they serve.
pub fn handle_request(sup: &Mutex<Supervisor>, payload: &Value) -> Result<MethodResult, String> {
    let object = match payload.as_object() {
        Some(object) => object,
        None => return Err("request must be a JSON object".to_string()),
    };

    let method = object.get("method").and_then(Value::as_str);
    let allowed_fields: &[&str] = match method {
        Some("start_runtime_profile") => &["argv"],
        Some("delete_auto_uv_profiles") => &["paths"],
        Some(name) if gpu_rpc::is_gpu_method(name) => gpu_rpc::allowed_fields(name),
        _ => &[],
    };

    let mut unknown: Vec<&str> = object
        .keys()
        .filter(|key| {
            let key = key.as_str();
            key != "method" && !allowed_fields.contains(&key)
        })
        .map(String::as_str)
        .collect();
    unknown.sort_unstable();
    if !unknown.is_empty() {
        return Err(format!("unknown request field: {}", unknown.join(", ")));
    }

    match method {
        Some("status") => Ok(MethodResult::Status(supervisor::status(sup))),
        Some("stop_auto_uv_scan") => Ok(MethodResult::Stop(supervisor::stop_auto_uv_scan(sup))),
        Some("stop_profile_verification") => Ok(MethodResult::Stop(
            supervisor::stop_profile_verification(sup),
        )),
        Some("start_runtime_profile") => {
            let argv = argvspec::parse_runtime_argv(object.get("argv"))?;
            supervisor::start_runtime_profile(sup, argv).map(MethodResult::Start)
        }
        Some("stop_runtime_profile") => {
            supervisor::stop_runtime_profile(sup).map(MethodResult::Stop)
        }
        Some("delete_auto_uv_profiles") => {
            delete::delete_auto_uv_profiles(object.get("paths")).map(MethodResult::Value)
        }
        Some("probe_power_limit_support") => {
            let gpu_index = gpu_rpc::request_gpu_index(object)?;
            if matches!(
                supervisor::active_child_kind(sup),
                Some(supervisor::ChildKind::Scan)
            ) {
                return Ok(MethodResult::Value(serde_json::json!({
                    "gpu_index": gpu_index,
                    "supported": false,
                    "reason": "auto-uv-scan-running",
                })));
            }
            gpu_rpc::probe_power_limit_support(object).map(MethodResult::Value)
        }
        Some(name) if gpu_rpc::is_gpu_method(name) => {
            gpu_rpc::handle(name, object).map(MethodResult::Value)
        }
        Some(name) if !name.is_empty() => Err(format!("unknown daemon method: {name}")),
        _ => Err("request method is required".to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn fresh() -> Mutex<Supervisor> {
        Mutex::new(Supervisor::new())
    }

    #[test]
    fn status_idle_shape() {
        let sup = fresh();
        let result = handle_request(&sup, &json!({"method": "status"})).unwrap();
        let text = serde_json::to_string(&OkEnvelope { ok: true, result }).unwrap();
        assert_eq!(
            text,
            format!(
                "{{\"ok\":true,\"result\":{{\"state\":\"idle\",\"active_job\":null,\"version\":\"{}\"}}}}",
                env!("CARGO_PKG_VERSION")
            )
        );
    }

    #[test]
    fn rejects_unknown_field() {
        let sup = fresh();
        let err = handle_request(&sup, &json!({"method": "status", "argv": ["--x"]})).unwrap_err();
        assert_eq!(err, "unknown request field: argv");
    }

    #[test]
    fn rejects_unknown_method() {
        let sup = fresh();
        let err = handle_request(&sup, &json!({"method": "run_cli"})).unwrap_err();
        assert_eq!(err, "unknown daemon method: run_cli");
    }

    #[test]
    fn requires_method() {
        let sup = fresh();
        assert_eq!(
            handle_request(&sup, &json!({})).unwrap_err(),
            "request method is required"
        );
        assert_eq!(
            handle_request(&sup, &json!({"method": ""})).unwrap_err(),
            "request method is required"
        );
        assert_eq!(
            handle_request(&sup, &json!({"method": 5})).unwrap_err(),
            "request method is required"
        );
    }

    #[test]
    fn rejects_non_object_request() {
        let sup = fresh();
        assert_eq!(
            handle_request(&sup, &json!(5)).unwrap_err(),
            "request must be a JSON object"
        );
    }

    #[test]
    fn start_runtime_profile_validates_argv_before_dispatch() {
        let sup = fresh();
        let err = handle_request(
            &sup,
            &json!({"method": "start_runtime_profile", "argv": ["--daemon-api", "/tmp/x"]}),
        )
        .unwrap_err();
        assert_eq!(err, "unsupported runtime profile argument: --daemon-api");
    }

    #[test]
    fn stop_runtime_profile_when_idle() {
        let sup = fresh();
        let result = handle_request(&sup, &json!({"method": "stop_runtime_profile"})).unwrap();
        let text = serde_json::to_string(&OkEnvelope { ok: true, result }).unwrap();
        assert_eq!(
            text,
            "{\"ok\":true,\"result\":{\"stopped\":false,\"state\":\"idle\"}}"
        );
    }

    #[test]
    fn stop_profile_verification_when_idle() {
        let sup = fresh();
        let result = handle_request(&sup, &json!({"method": "stop_profile_verification"})).unwrap();
        let text = serde_json::to_string(&OkEnvelope { ok: true, result }).unwrap();
        assert_eq!(
            text,
            "{\"ok\":true,\"result\":{\"stopped\":false,\"state\":\"idle\"}}"
        );
    }

    #[test]
    fn gpu_method_rejects_unknown_field_and_bad_gpu_index() {
        let sup = fresh();
        // Both failures happen before any backend is opened (no NVML in tests).
        let err = handle_request(
            &sup,
            &json!({"method": "gpu_apply_power_limit", "gpu_index": 0, "power_limit_w": 1, "bogus": 1}),
        )
        .unwrap_err();
        assert_eq!(err, "unknown request field: bogus");

        let err = handle_request(
            &sup,
            &json!({"method": "gpu_apply_power_limit", "gpu_index": "x", "power_limit_w": 1}),
        )
        .unwrap_err();
        assert_eq!(err, "gpu_index must be an integer");
    }

    #[test]
    fn probe_power_limit_support_validates_request_before_backend() {
        let sup = fresh();
        let err = handle_request(
            &sup,
            &json!({"method": "probe_power_limit_support", "gpu_index": 0, "bogus": true}),
        )
        .unwrap_err();
        assert_eq!(err, "unknown request field: bogus");

        let err = handle_request(
            &sup,
            &json!({"method": "probe_power_limit_support", "gpu_index": "0"}),
        )
        .unwrap_err();
        assert_eq!(err, "gpu_index must be an integer");
    }

    #[test]
    fn delete_auto_uv_profiles_requires_a_string_list() {
        let sup = fresh();
        let err = handle_request(&sup, &json!({"method": "delete_auto_uv_profiles"})).unwrap_err();
        assert_eq!(err, "profile paths must be a JSON string list");
        let err = handle_request(
            &sup,
            &json!({"method": "delete_auto_uv_profiles", "paths": [1]}),
        )
        .unwrap_err();
        assert_eq!(err, "profile paths must be a JSON string list");
        // Unknown fields are rejected before any filesystem work.
        let err = handle_request(
            &sup,
            &json!({"method": "delete_auto_uv_profiles", "paths": [], "force": true}),
        )
        .unwrap_err();
        assert_eq!(err, "unknown request field: force");
    }
}
