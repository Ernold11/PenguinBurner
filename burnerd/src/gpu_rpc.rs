//! GPU-write RPCs (milestone B1): thin socket exposure of the existing
//! [`GpuBackend`] write methods, so the Python Auto-UV sweep keeps its logic
//! byte-identical while every GPU mutation is implemented once, in Rust.
//!
//! Backend: one lazy shared `NvmlBackend` per `gpu_index`, opened on first use
//! and kept for the daemon's lifetime (NVML is refcounted/thread-safe; the
//! hidden-NVAPI get-mutate-set stays per-call inside the backend). All access
//! is serialized under one registry mutex — writes are slow NVML/NVAPI ops and
//! the scan child is the only intended caller, so contention is irrelevant.
//!
//! Failures relay the backend's exact `Display` text in the standard
//! `{"ok":false,"error":…}` envelope: the Python initial-check pattern-matches
//! these strings (`_looks_like_permission_error`), so message fidelity is
//! load-bearing.
//!
//! Test seam (never active in production): `PENGUIN_BURNERD_TEST_MOCK_GPU=1`
//! swaps the lazy backend for a canned [`MockGpu`] (supported core-clock steps
//! 1800/1900/2000/2100 MHz) and echoes the ops recorded during the call as a
//! `mock_ops` list in the result so integration tests can assert the exact
//! backend calls; `PENGUIN_BURNERD_TEST_MOCK_GPU_FAIL=<trait method>` injects a
//! `"<method> mock failure"` error to exercise the error-relay path.

use std::env;
use std::sync::Mutex;

use serde_json::{json, Map, Value};

use crate::gpu::{mock::MockGpu, GpuBackend, GpuError, NvmlBackend};

const MOCK_ENV: &str = "PENGUIN_BURNERD_TEST_MOCK_GPU";
const MOCK_FAIL_ENV: &str = "PENGUIN_BURNERD_TEST_MOCK_GPU_FAIL";

/// method → request fields allowed besides `method`. Also the method registry:
/// a name missing here is "unknown daemon method".
const METHODS: &[(&str, &[&str])] = &[
    ("gpu_apply_vf_offsets", &["gpu_index", "offsets"]),
    ("gpu_apply_power_limit", &["gpu_index", "power_limit_w"]),
    (
        "gpu_apply_clock_offsets",
        &[
            "gpu_index",
            "gpc_clk_vf_offset_mhz",
            "mem_clk_vf_offset_mhz",
        ],
    ),
    (
        "gpu_apply_locked_core_clock",
        &[
            "gpu_index",
            "clock_mhz",
            "prefer_not_above",
            "snap_to_supported",
        ],
    ),
    (
        "gpu_apply_locked_core_clock_range",
        &[
            "gpu_index",
            "min_mhz",
            "max_mhz",
            "prefer_max_not_above",
            "snap_to_supported",
        ],
    ),
    ("gpu_reset_locked_core_clocks", &["gpu_index"]),
    ("gpu_reset_locked_memory_clocks", &["gpu_index"]),
    ("gpu_enable_persistence_mode", &["gpu_index"]),
];

pub fn is_gpu_method(method: &str) -> bool {
    METHODS.iter().any(|(name, _)| *name == method)
}

pub fn allowed_fields(method: &str) -> &'static [&'static str] {
    METHODS
        .iter()
        .find(|(name, _)| *name == method)
        .map(|(_, fields)| *fields)
        .unwrap_or(&[])
}

enum RpcBackend {
    Real(NvmlBackend),
    Mock(MockGpu),
}

impl RpcBackend {
    fn backend(&self) -> &dyn GpuBackend {
        match self {
            RpcBackend::Real(backend) => backend,
            RpcBackend::Mock(mock) => mock,
        }
    }
}

// SAFETY: the registry `Mutex` serializes every access, so the backend is only
// ever touched by one thread at a time — no data race on its interior
// `Cell`/`RefCell` caches. Cross-thread *reuse* (opened on one connection
// thread, called from another) is sound because the state it holds is not
// thread-affine: the NVML library init is refcounted process-global and NVML
// documents its API as thread-safe, and the hidden-NVAPI sessions hold only a
// process-global `dlopen` handle, process-global function pointers, and a
// process-stable physical-GPU handle (the same properties that let the former
// threaded Python daemon call these drivers from arbitrary handler threads).
unsafe impl Send for RpcBackend {}

/// Lazy per-`gpu_index` backends, kept for the daemon's lifetime.
static REGISTRY: Mutex<Vec<(u32, RpcBackend)>> = Mutex::new(Vec::new());

fn open_backend(gpu_index: u32) -> Result<RpcBackend, String> {
    if env::var_os(MOCK_ENV).is_some_and(|v| !v.is_empty()) {
        let mut mock = MockGpu::new();
        mock.gpu_index = gpu_index;
        mock.supported_core_clocks = vec![1800, 1900, 2000, 2100];
        if let Ok(method) = env::var(MOCK_FAIL_ENV) {
            if !method.is_empty() {
                let message = format!("{method} mock failure");
                mock.inject_failure(
                    Box::leak(method.into_boxed_str()),
                    GpuError::other(message, 0),
                );
            }
        }
        return Ok(RpcBackend::Mock(mock));
    }
    NvmlBackend::open(gpu_index)
        .map(RpcBackend::Real)
        .map_err(|err| err.to_string())
}

/// A fully-parsed, validated GPU write. Parsing is pure (no backend), so every
/// field-validation error is returned BEFORE any NVML/NVAPI init.
enum GpuWrite {
    VfOffsets(Vec<(u32, i32)>),
    PowerLimit(i64),
    ClockOffsets {
        gpc: Option<i32>,
        mem: Option<i32>,
    },
    LockedCoreClock {
        clock_mhz: i64,
        prefer_not_above: bool,
        snap: bool,
    },
    LockedCoreClockRange {
        min_mhz: i64,
        max_mhz: i64,
        prefer_max_not_above: bool,
        snap: bool,
    },
    ResetLockedCore,
    ResetLockedMemory,
    EnablePersistence,
}

/// Dispatch one `gpu_*` request. `request` is the full request object (with
/// `method`); unknown-field rejection already happened in `api::handle_request`.
///
/// Supervisor interplay: GPU writes are deliberately legal in ANY supervisor
/// state — while a scan or verification runs the engine is stopped by design
/// and the child is the intended caller; there is no further arbitration.
pub fn handle(method: &str, request: &Map<String, Value>) -> Result<Value, String> {
    // Validate everything (gpu_index + method fields) before opening a backend,
    // so a bad request never pays an NVML init nor masks the field error with
    // an NVML-open error on a GPU-less machine.
    let gpu_index = require_u32(request, "gpu_index")?;
    let write = parse(method, request)?;

    let mut registry = REGISTRY.lock().unwrap_or_else(|poison| poison.into_inner());
    let position = match registry.iter().position(|(index, _)| *index == gpu_index) {
        Some(position) => position,
        None => {
            registry.push((gpu_index, open_backend(gpu_index)?));
            registry.len() - 1
        }
    };
    let entry = &registry[position].1;

    let ops_before = match entry {
        RpcBackend::Mock(mock) => Some(mock.recorded().len()),
        RpcBackend::Real(_) => None,
    };
    let mut result = execute(entry.backend(), write)?;
    if let (Some(before), RpcBackend::Mock(mock)) = (ops_before, entry) {
        let ops: Vec<Value> = mock.recorded()[before..]
            .iter()
            .map(|op| Value::String(format!("{op:?}")))
            .collect();
        if let Some(object) = result.as_object_mut() {
            object.insert("mock_ops".to_string(), Value::Array(ops));
        }
    }
    Ok(result)
}

/// Parse + validate the method's request fields into a [`GpuWrite`]. Pure.
fn parse(method: &str, request: &Map<String, Value>) -> Result<GpuWrite, String> {
    Ok(match method {
        "gpu_apply_vf_offsets" => GpuWrite::VfOffsets(require_offsets(request)?),
        "gpu_apply_power_limit" => GpuWrite::PowerLimit(require_i64(request, "power_limit_w")?),
        "gpu_apply_clock_offsets" => GpuWrite::ClockOffsets {
            gpc: optional_i32(request, "gpc_clk_vf_offset_mhz")?,
            mem: optional_i32(request, "mem_clk_vf_offset_mhz")?,
        },
        "gpu_apply_locked_core_clock" => GpuWrite::LockedCoreClock {
            clock_mhz: require_i64(request, "clock_mhz")?,
            prefer_not_above: optional_bool(request, "prefer_not_above")?.unwrap_or(true),
            snap: optional_bool(request, "snap_to_supported")?.unwrap_or(true),
        },
        "gpu_apply_locked_core_clock_range" => GpuWrite::LockedCoreClockRange {
            min_mhz: require_i64(request, "min_mhz")?,
            max_mhz: require_i64(request, "max_mhz")?,
            prefer_max_not_above: optional_bool(request, "prefer_max_not_above")?.unwrap_or(true),
            snap: optional_bool(request, "snap_to_supported")?.unwrap_or(true),
        },
        "gpu_reset_locked_core_clocks" => GpuWrite::ResetLockedCore,
        "gpu_reset_locked_memory_clocks" => GpuWrite::ResetLockedMemory,
        "gpu_enable_persistence_mode" => GpuWrite::EnablePersistence,
        other => return Err(format!("unknown daemon method: {other}")),
    })
}

/// Run a parsed write against `backend`, relaying the backend's exact error
/// text and returning the method's result dict.
fn execute(backend: &dyn GpuBackend, write: GpuWrite) -> Result<Value, String> {
    match write {
        GpuWrite::VfOffsets(offsets) => {
            backend
                .apply_vf_offsets_khz(&offsets)
                .map_err(|err| err.to_string())?;
            Ok(json!({ "applied": offsets.len() }))
        }
        GpuWrite::PowerLimit(watts) => {
            let applied = backend
                .apply_power_limit_w(watts)
                .map_err(|err| err.to_string())?;
            Ok(json!({ "applied_w": applied }))
        }
        GpuWrite::ClockOffsets { gpc, mem } => {
            let applied = backend
                .apply_clock_offsets(gpc, mem)
                .map_err(|err| err.to_string())?;
            // Mirror the Python dict: only the requested sides carry keys, the
            // mandatory read-back may be null (issue #20: it can differ).
            let mut object = Map::new();
            if let Some(value) = applied.gpc_clk_vf_offset_mhz {
                object.insert("gpc_clk_vf_offset_mhz".to_string(), json!(value));
                object.insert(
                    "gpc_clk_vf_offset_readback_mhz".to_string(),
                    json!(applied.gpc_clk_vf_offset_readback_mhz),
                );
            }
            if let Some(value) = applied.mem_clk_vf_offset_mhz {
                object.insert("mem_clk_vf_offset_mhz".to_string(), json!(value));
                object.insert(
                    "mem_clk_vf_offset_readback_mhz".to_string(),
                    json!(applied.mem_clk_vf_offset_readback_mhz),
                );
            }
            Ok(Value::Object(object))
        }
        GpuWrite::LockedCoreClock {
            clock_mhz,
            prefer_not_above,
            snap,
        } => {
            let result = backend
                .apply_locked_core_clock_mhz(clock_mhz, prefer_not_above, snap)
                .map_err(|err| err.to_string())?;
            // The snap struct's field names ARE the wire keys (Serialize derive).
            Ok(serde_json::to_value(result).expect("SnapResult serializes"))
        }
        GpuWrite::LockedCoreClockRange {
            min_mhz,
            max_mhz,
            prefer_max_not_above,
            snap,
        } => {
            let result = backend
                .apply_locked_core_clock_range_mhz(min_mhz, max_mhz, prefer_max_not_above, snap)
                .map_err(|err| err.to_string())?;
            Ok(serde_json::to_value(result).expect("RangeSnapResult serializes"))
        }
        GpuWrite::ResetLockedCore => {
            backend
                .reset_locked_core_clocks()
                .map_err(|err| err.to_string())?;
            Ok(json!({ "reset": true }))
        }
        GpuWrite::ResetLockedMemory => {
            backend
                .reset_locked_memory_clocks()
                .map_err(|err| err.to_string())?;
            Ok(json!({ "reset": true }))
        }
        GpuWrite::EnablePersistence => {
            backend
                .enable_persistence_mode()
                .map_err(|err| err.to_string())?;
            Ok(json!({ "enabled": true }))
        }
    }
}

// --- request field validation -------------------------------------------
// Type errors use one fixed message per field so clients get a stable string;
// missing and mistyped are deliberately the same error.

fn require_u32(request: &Map<String, Value>, field: &str) -> Result<u32, String> {
    request
        .get(field)
        .and_then(Value::as_u64)
        .and_then(|value| u32::try_from(value).ok())
        .ok_or_else(|| format!("{field} must be an integer"))
}

fn require_i64(request: &Map<String, Value>, field: &str) -> Result<i64, String> {
    request
        .get(field)
        .and_then(Value::as_i64)
        .ok_or_else(|| format!("{field} must be an integer"))
}

/// Optional signed-MHz offset: absent or `null` → `None` (Python kwarg `None`).
fn optional_i32(request: &Map<String, Value>, field: &str) -> Result<Option<i32>, String> {
    match request.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(value) => value
            .as_i64()
            .and_then(|v| i32::try_from(v).ok())
            .map(Some)
            .ok_or_else(|| format!("{field} must be an integer")),
    }
}

fn optional_bool(request: &Map<String, Value>, field: &str) -> Result<Option<bool>, String> {
    match request.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Bool(flag)) => Ok(Some(*flag)),
        Some(_) => Err(format!("{field} must be a boolean")),
    }
}

/// `offsets`: a list of `[index, offset_khz]` integer pairs (the VF plan). The
/// backend owns the NVAPI get-mutate-set and preserves non-listed points.
fn require_offsets(request: &Map<String, Value>) -> Result<Vec<(u32, i32)>, String> {
    const ERR: &str = "offsets must be a list of [index, offset_khz] integer pairs";
    let items = request
        .get("offsets")
        .and_then(Value::as_array)
        .ok_or_else(|| ERR.to_string())?;
    let mut offsets = Vec::with_capacity(items.len());
    for item in items {
        let pair = item.as_array().filter(|p| p.len() == 2);
        let index = pair
            .and_then(|p| p[0].as_u64())
            .and_then(|v| u32::try_from(v).ok());
        let offset = pair
            .and_then(|p| p[1].as_i64())
            .and_then(|v| i32::try_from(v).ok());
        match (index, offset) {
            (Some(index), Some(offset)) => offsets.push((index, offset)),
            _ => return Err(ERR.to_string()),
        }
    }
    Ok(offsets)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::gpu::mock::MockGpu;

    fn request(json_text: &str) -> Map<String, Value> {
        serde_json::from_str::<Value>(json_text)
            .unwrap()
            .as_object()
            .unwrap()
            .clone()
    }

    fn mock() -> MockGpu {
        let mut mock = MockGpu::new();
        mock.supported_core_clocks = vec![1800, 1900, 2000, 2100];
        mock
    }

    /// Test shim: parse + execute in one call. Production splits them so field
    /// validation runs before any backend opens.
    fn dispatch(
        backend: &dyn GpuBackend,
        method: &str,
        request: &Map<String, Value>,
    ) -> Result<Value, String> {
        execute(backend, parse(method, request)?)
    }

    #[test]
    fn method_registry_and_fields() {
        assert!(is_gpu_method("gpu_apply_vf_offsets"));
        assert!(is_gpu_method("gpu_enable_persistence_mode"));
        assert!(!is_gpu_method("gpu_frobnicate"));
        assert!(!is_gpu_method("status"));
        assert_eq!(
            allowed_fields("gpu_apply_power_limit"),
            &["gpu_index", "power_limit_w"]
        );
        assert!(allowed_fields("nope").is_empty());
    }

    #[test]
    fn vf_offsets_apply_and_count() {
        let mock = mock();
        let req = request(r#"{"gpu_index":0,"offsets":[[12,150000],[13,-15000]]}"#);
        let result = dispatch(&mock, "gpu_apply_vf_offsets", &req).unwrap();
        assert_eq!(result, json!({"applied": 2}));
        assert_eq!(
            mock.recorded(),
            vec![crate::gpu::mock::MockOp::ApplyVfOffsets {
                offsets: vec![(12, 150_000), (13, -15_000)]
            }]
        );
    }

    #[test]
    fn vf_offsets_validation_rejects_bad_shapes() {
        let mock = mock();
        const ERR: &str = "offsets must be a list of [index, offset_khz] integer pairs";
        for bad in [
            r#"{"gpu_index":0}"#,                            // missing
            r#"{"gpu_index":0,"offsets":5}"#,                // not a list
            r#"{"gpu_index":0,"offsets":[[1]]}"#,            // not a pair
            r#"{"gpu_index":0,"offsets":[[1,2,3]]}"#,        // too long
            r#"{"gpu_index":0,"offsets":[[1,"x"]]}"#,        // non-int offset
            r#"{"gpu_index":0,"offsets":[[-1,0]]}"#,         // negative index
            r#"{"gpu_index":0,"offsets":[[1,1.5]]}"#,        // float offset
            r#"{"gpu_index":0,"offsets":[[1,2147483648]]}"#, // offset > i32
            r#"{"gpu_index":0,"offsets":["x"]}"#,            // non-array item
        ] {
            let err = dispatch(&mock, "gpu_apply_vf_offsets", &request(bad)).unwrap_err();
            assert_eq!(err, ERR, "case: {bad}");
        }
        // Nothing reached the backend on validation failure.
        assert!(mock.recorded().is_empty());
    }

    #[test]
    fn empty_offsets_list_is_a_no_op_apply() {
        let mock = mock();
        let req = request(r#"{"gpu_index":0,"offsets":[]}"#);
        let result = dispatch(&mock, "gpu_apply_vf_offsets", &req).unwrap();
        assert_eq!(result, json!({"applied": 0}));
    }

    #[test]
    fn power_limit_applies_and_validates() {
        let mock = mock();
        let req = request(r#"{"gpu_index":0,"power_limit_w":300}"#);
        let result = dispatch(&mock, "gpu_apply_power_limit", &req).unwrap();
        assert_eq!(result, json!({"applied_w": 300}));

        for bad in [
            r#"{"gpu_index":0}"#,
            r#"{"gpu_index":0,"power_limit_w":"300"}"#,
            r#"{"gpu_index":0,"power_limit_w":300.5}"#,
            r#"{"gpu_index":0,"power_limit_w":true}"#,
            r#"{"gpu_index":0,"power_limit_w":null}"#,
        ] {
            let err = dispatch(&mock, "gpu_apply_power_limit", &request(bad)).unwrap_err();
            assert_eq!(err, "power_limit_w must be an integer", "case: {bad}");
        }
    }

    #[test]
    fn clock_offsets_mirror_requested_sides_only() {
        let mock = mock();
        let both = dispatch(
            &mock,
            "gpu_apply_clock_offsets",
            &request(r#"{"gpu_index":0,"gpc_clk_vf_offset_mhz":30,"mem_clk_vf_offset_mhz":1500}"#),
        )
        .unwrap();
        assert_eq!(
            both,
            json!({
                "gpc_clk_vf_offset_mhz": 30,
                "gpc_clk_vf_offset_readback_mhz": 30,
                "mem_clk_vf_offset_mhz": 1500,
                "mem_clk_vf_offset_readback_mhz": 1500,
            })
        );

        let gpc_only = dispatch(
            &mock,
            "gpu_apply_clock_offsets",
            &request(r#"{"gpu_index":0,"gpc_clk_vf_offset_mhz":0,"mem_clk_vf_offset_mhz":null}"#),
        )
        .unwrap();
        assert_eq!(
            gpc_only,
            json!({"gpc_clk_vf_offset_mhz": 0, "gpc_clk_vf_offset_readback_mhz": 0})
        );

        // No sides requested → empty dict (Python parity).
        let none = dispatch(
            &mock,
            "gpu_apply_clock_offsets",
            &request(r#"{"gpu_index":0}"#),
        )
        .unwrap();
        assert_eq!(none, json!({}));

        let err = dispatch(
            &mock,
            "gpu_apply_clock_offsets",
            &request(r#"{"gpu_index":0,"gpc_clk_vf_offset_mhz":"x"}"#),
        )
        .unwrap_err();
        assert_eq!(err, "gpc_clk_vf_offset_mhz must be an integer");
    }

    #[test]
    fn mem_readback_divergence_is_relayed() {
        let mut mock = mock();
        mock.mem_offset_readback = Some(0); // issue #20: write "succeeds", does not stick
        let result = dispatch(
            &mock,
            "gpu_apply_clock_offsets",
            &request(r#"{"gpu_index":0,"mem_clk_vf_offset_mhz":1500}"#),
        )
        .unwrap();
        assert_eq!(
            result,
            json!({"mem_clk_vf_offset_mhz": 1500, "mem_clk_vf_offset_readback_mhz": 0})
        );
    }

    #[test]
    fn locked_core_clock_returns_snap_dict_and_defaults() {
        let mock = mock();
        let snap = dispatch(
            &mock,
            "gpu_apply_locked_core_clock",
            &request(r#"{"gpu_index":0,"clock_mhz":1950}"#),
        )
        .unwrap();
        // Defaults: prefer_not_above=true, snap_to_supported=true → floor.
        assert_eq!(
            snap,
            json!({
                "requested_clock_mhz": 1950,
                "applied_clock_mhz": 1900,
                "mode": "floor",
                "supported_steps_mhz": [1800, 1900, 2000, 2100],
            })
        );

        let unsnapped = dispatch(
            &mock,
            "gpu_apply_locked_core_clock",
            &request(r#"{"gpu_index":0,"clock_mhz":1950,"snap_to_supported":false}"#),
        )
        .unwrap();
        assert_eq!(unsnapped["mode"], "unsnapped");
        assert_eq!(unsnapped["applied_clock_mhz"], 1950);

        let err = dispatch(
            &mock,
            "gpu_apply_locked_core_clock",
            &request(r#"{"gpu_index":0,"clock_mhz":1950,"prefer_not_above":1}"#),
        )
        .unwrap_err();
        assert_eq!(err, "prefer_not_above must be a boolean");
        let err = dispatch(
            &mock,
            "gpu_apply_locked_core_clock",
            &request(r#"{"gpu_index":0,"clock_mhz":"fast"}"#),
        )
        .unwrap_err();
        assert_eq!(err, "clock_mhz must be an integer");
    }

    #[test]
    fn locked_core_clock_range_returns_range_snap_dict() {
        let mock = mock();
        let range = dispatch(
            &mock,
            "gpu_apply_locked_core_clock_range",
            &request(
                r#"{"gpu_index":0,"min_mhz":1850,"max_mhz":2150,"prefer_max_not_above":true}"#,
            ),
        )
        .unwrap();
        assert_eq!(
            range,
            json!({
                "requested_min_clock_mhz": 1850,
                "requested_max_clock_mhz": 2150,
                "applied_min_clock_mhz": 1900,
                "applied_max_clock_mhz": 2100,
                "min_mode": "ceil",
                "max_mode": "floor",
                "supported_steps_mhz": [1800, 1900, 2000, 2100],
            })
        );

        let err = dispatch(
            &mock,
            "gpu_apply_locked_core_clock_range",
            &request(r#"{"gpu_index":0,"min_mhz":1850}"#),
        )
        .unwrap_err();
        assert_eq!(err, "max_mhz must be an integer");
    }

    #[test]
    fn resets_and_persistence() {
        let mock = mock();
        assert_eq!(
            dispatch(
                &mock,
                "gpu_reset_locked_core_clocks",
                &request(r#"{"gpu_index":0}"#)
            )
            .unwrap(),
            json!({"reset": true})
        );
        assert_eq!(
            dispatch(
                &mock,
                "gpu_reset_locked_memory_clocks",
                &request(r#"{"gpu_index":0}"#)
            )
            .unwrap(),
            json!({"reset": true})
        );
        assert_eq!(
            dispatch(
                &mock,
                "gpu_enable_persistence_mode",
                &request(r#"{"gpu_index":0}"#)
            )
            .unwrap(),
            json!({"enabled": true})
        );
        assert_eq!(
            mock.recorded(),
            vec![
                crate::gpu::mock::MockOp::ResetLockedCoreClocks,
                crate::gpu::mock::MockOp::ResetLockedMemoryClocks,
                crate::gpu::mock::MockOp::EnablePersistence,
            ]
        );
    }

    #[test]
    fn backend_error_text_is_relayed_verbatim() {
        let mock = mock();
        mock.inject_failure(
            "apply_power_limit_w",
            GpuError::nvml_with_text(
                "nvmlDeviceSetPowerManagementLimit",
                4,
                "Insufficient Permissions",
            ),
        );
        let err = dispatch(
            &mock,
            "gpu_apply_power_limit",
            &request(r#"{"gpu_index":0,"power_limit_w":300}"#),
        )
        .unwrap_err();
        // The exact text `_looks_like_permission_error` matches ("nvml error 4").
        assert_eq!(
            err,
            "nvmlDeviceSetPowerManagementLimit failed with NVML error 4: Insufficient Permissions"
        );

        mock.inject_failure(
            "apply_vf_offsets_khz",
            GpuError::nvapi(
                "ClockClientClkVfPointsSetControl",
                -104,
                "invalid_user_privilege",
            ),
        );
        let err = dispatch(
            &mock,
            "gpu_apply_vf_offsets",
            &request(r#"{"gpu_index":0,"offsets":[[1,0]]}"#),
        )
        .unwrap_err();
        assert_eq!(
            err,
            "ClockClientClkVfPointsSetControl failed with status -104: invalid_user_privilege"
        );
    }

    #[test]
    fn gpu_index_validation() {
        for bad in [
            r#"{"power_limit_w":300}"#,
            r#"{"gpu_index":-1,"power_limit_w":300}"#,
            r#"{"gpu_index":0.5,"power_limit_w":300}"#,
            r#"{"gpu_index":"0","power_limit_w":300}"#,
            r#"{"gpu_index":4294967296,"power_limit_w":300}"#,
        ] {
            let err = require_u32(&request(bad), "gpu_index").unwrap_err();
            assert_eq!(err, "gpu_index must be an integer", "case: {bad}");
        }
        assert_eq!(request(r#"{"gpu_index":1}"#).len(), 1);
        assert_eq!(
            require_u32(&request(r#"{"gpu_index":1}"#), "gpu_index"),
            Ok(1)
        );
    }
}
