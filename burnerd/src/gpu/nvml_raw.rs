//! Raw `libnvidia-ml.so.1` FFI: `dlopen` via `libloading`, symbol resolution
//! with the exact fallback chains, and version-tagged `#[repr(C)]` structs.
//!
//! `nvml-wrapper` collapses `nvmlReturn_t` into a typed enum, discarding the
//! integer return code that PenguinBurner's error messages embed verbatim; it
//! also lacks the VF-offset / min-max-VF-offset / throttle-reason symbols
//! entirely. So the whole NVML surface is loaded here by hand, preserving the
//! numeric `rc`, the optional-symbol degradation, and the `_v3→_v2` /
//! `count_v2→count` / `ThrottleReasons→EventReasons` chains (spec 03 §5/§12).

use std::ffi::c_void;
use std::os::raw::{c_char, c_int, c_uint, c_ulonglong};

use libloading::{Library, Symbol};

use super::{first_available, GpuError};

/// Opaque `nvmlDevice_t`.
pub(crate) type Device = *mut c_void;

// --- FFI signature aliases (all `restype = c_int` unless noted) ------------
pub(crate) type FnRet = unsafe extern "C" fn() -> c_int;
pub(crate) type FnHandle = unsafe extern "C" fn(c_uint, *mut Device) -> c_int;
pub(crate) type FnDev = unsafe extern "C" fn(Device) -> c_int;
pub(crate) type FnDevUout = unsafe extern "C" fn(Device, *mut c_uint) -> c_int;
pub(crate) type FnDevUUout = unsafe extern "C" fn(Device, c_uint, *mut c_uint) -> c_int;
pub(crate) type FnDevUUout2 = unsafe extern "C" fn(Device, *mut c_uint, *mut c_uint) -> c_int;
pub(crate) type FnGfxClocks =
    unsafe extern "C" fn(Device, c_uint, *mut c_uint, *mut c_uint) -> c_int;
pub(crate) type FnDevUin = unsafe extern "C" fn(Device, c_uint) -> c_int;
pub(crate) type FnDevUUin = unsafe extern "C" fn(Device, c_uint, c_uint) -> c_int;
pub(crate) type FnUout = unsafe extern "C" fn(*mut c_uint) -> c_int;
pub(crate) type FnCharU = unsafe extern "C" fn(*mut c_char, c_uint) -> c_int;
pub(crate) type FnDevCharU = unsafe extern "C" fn(Device, *mut c_char, c_uint) -> c_int;
pub(crate) type FnDevPci = unsafe extern "C" fn(Device, *mut NvmlPciInfo) -> c_int;
pub(crate) type FnDevMem = unsafe extern "C" fn(Device, *mut NvmlMemoryInfo) -> c_int;
pub(crate) type FnDevUtil = unsafe extern "C" fn(Device, *mut NvmlUtilization) -> c_int;
pub(crate) type FnErrStr = unsafe extern "C" fn(c_int) -> *const c_char;
pub(crate) type FnDevU64 = unsafe extern "C" fn(Device, *mut c_ulonglong) -> c_int;
pub(crate) type FnDevIout = unsafe extern "C" fn(Device, *mut c_int) -> c_int;
pub(crate) type FnDevIin = unsafe extern "C" fn(Device, c_int) -> c_int;
pub(crate) type FnDevIIout2 = unsafe extern "C" fn(Device, *mut c_int, *mut c_int) -> c_int;
pub(crate) type FnDevClockOffset = unsafe extern "C" fn(Device, *mut NvmlClockOffset) -> c_int;

// --- version-tagged structs (sizes asserted against ctypes `sizeof`) -------

/// `nvmlPciInfo_t` (matches `nvml_identity.py::NvmlPciInfo`, 68 bytes).
#[repr(C)]
pub(crate) struct NvmlPciInfo {
    pub bus_id_legacy: [u8; 16],
    pub domain: c_uint,
    pub bus: c_uint,
    pub device: c_uint,
    pub pci_device_id: c_uint,
    pub pci_sub_system_id: c_uint,
    pub bus_id: [u8; 32],
}
const _: () = assert!(std::mem::size_of::<NvmlPciInfo>() == 68);

/// `nvmlMemory_t` (24 bytes).
#[repr(C)]
pub(crate) struct NvmlMemoryInfo {
    pub total: c_ulonglong,
    pub free: c_ulonglong,
    pub used: c_ulonglong,
}
const _: () = assert!(std::mem::size_of::<NvmlMemoryInfo>() == 24);

/// `nvmlUtilization_t` (8 bytes).
#[repr(C)]
pub(crate) struct NvmlUtilization {
    pub gpu: c_uint,
    pub memory: c_uint,
}
const _: () = assert!(std::mem::size_of::<NvmlUtilization>() == 8);

/// `nvmlClockOffset_v1_t` (24 bytes): one coarse offset per (clock domain,
/// pstate) — the R555+ replacement for the deprecated per-domain VF-offset
/// calls (spec 17). On Get the driver fills cur/min/max for the tagged
/// (type, pstate); on Set only `clock_offset_mhz` is consumed.
#[repr(C)]
pub(crate) struct NvmlClockOffset {
    pub version: c_uint,             // nvmlClockOffset_v1
    pub clock_type: c_int,           // nvmlClockType_t: 0 = GPC, 2 = MEM
    pub pstate: c_int,               // nvmlPstates_t: 0 = P0
    pub clock_offset_mhz: c_int,     // IN on Set / OUT on Get (signed MHz)
    pub min_clock_offset_mhz: c_int, // OUT
    pub max_clock_offset_mhz: c_int, // OUT
}
const _: () = assert!(std::mem::size_of::<NvmlClockOffset>() == 24);

/// NVML struct version tag: `sizeof | (ver << 24)`. NVML shifts the version
/// into bits 24+, unlike NVAPI's `make_version` (`ver << 16`) — do not reuse
/// that helper. A wrong tag returns NVML_ERROR_ARGUMENT_VERSION_MISMATCH (25).
pub(crate) const fn nvml_struct_version(size: usize, ver: u32) -> u32 {
    (size as u32) | (ver << 24)
}
pub(crate) const NVML_CLOCK_OFFSET_V1: u32 =
    nvml_struct_version(std::mem::size_of::<NvmlClockOffset>(), 1); // 0x0100_0018

pub(crate) const NVML_CLOCK_GRAPHICS: c_int = 0;
pub(crate) const NVML_CLOCK_MEM: c_int = 2;
pub(crate) const NVML_PSTATE_0: c_int = 0;

impl NvmlClockOffset {
    /// A version-tagged struct targeting `clock_type` at P0 — the load pstate
    /// the profiles act on. Ready for Get as-is; set `clock_offset_mhz` for Set.
    pub(crate) fn query_p0(clock_type: c_int) -> Self {
        Self {
            version: NVML_CLOCK_OFFSET_V1,
            clock_type,
            pstate: NVML_PSTATE_0,
            clock_offset_mhz: 0,
            min_clock_offset_mhz: 0,
            max_clock_offset_mhz: 0,
        }
    }
}

// --- fallback chains (data, so selection is unit-testable) -----------------
const COUNT_SYMS: &[&str] = &["nvmlDeviceGetCount_v2", "nvmlDeviceGetCount"];
const PCI_SYMS: &[&str] = &["nvmlDeviceGetPciInfo_v3", "nvmlDeviceGetPciInfo_v2"];
const THROTTLE_SYMS: &[&str] = &[
    "nvmlDeviceGetCurrentClocksThrottleReasons",
    "nvmlDeviceGetCurrentClocksEventReasons",
];

/// Loaded NVML library plus resolved function pointers. Required symbols are
/// bare (bind failure aborts construction); optional symbols are `Option`
/// (missing → the feature degrades).
pub(crate) struct NvmlLib {
    // Keep the library alive for the lifetime of the extracted pointers.
    _lib: Library,

    // Required.
    pub init_v2: FnRet,
    pub shutdown: FnRet,
    pub handle_by_index: FnHandle,
    pub get_temperature: FnDevUUout,
    pub get_num_fans: FnDevUout,
    pub get_fan_speed: FnDevUout,
    pub get_fan_speed_v2: FnDevUUout,
    pub get_power_usage: FnDevUout,
    pub get_clock_info: FnDevUUout,

    // Optional — telemetry / identity. Several are bound but only read by the
    // milestone-B identity/count/throttle reads, unused by the A3 engine.
    pub error_string: Option<FnErrStr>,
    #[allow(dead_code)]
    pub get_count: Option<FnUout>,
    pub get_pci_info: Option<FnDevPci>,
    #[allow(dead_code)]
    pub get_mem_info: Option<FnDevMem>,
    #[allow(dead_code)]
    pub get_name: Option<FnDevCharU>,
    #[allow(dead_code)]
    pub get_uuid: Option<FnDevCharU>,
    #[allow(dead_code)]
    pub get_driver: Option<FnCharU>,
    pub get_util: Option<FnDevUtil>,
    #[allow(dead_code)]
    pub get_throttle: Option<FnDevU64>,
    pub get_minmax_fan: Option<FnDevUUout2>,

    // Optional — fan / power / clock writes and queries.
    pub set_fan_speed_v2: Option<FnDevUUin>,
    pub set_default_fan: Option<FnDevUin>,
    pub get_pm_limit: Option<FnDevUout>,
    pub get_pm_default: Option<FnDevUout>,
    pub get_pm_constraints: Option<FnDevUUout2>,
    pub set_pm_limit: Option<FnDevUin>,
    pub set_persistence: Option<FnDevUin>,
    pub set_locked_clocks: Option<FnDevUUin>,
    pub reset_locked_core: Option<FnDev>,
    pub reset_locked_mem: Option<FnDev>,
    pub get_supported_mem_clocks: Option<FnDevUUout2>,
    pub get_supported_gfx_clocks: Option<FnGfxClocks>,
    pub get_mem_minmax_vf: Option<FnDevIIout2>,
    pub get_gpc_vf: Option<FnDevIout>,
    pub set_gpc_vf: Option<FnDevIin>,
    pub get_mem_vf: Option<FnDevIout>,
    pub set_mem_vf: Option<FnDevIin>,

    // Per-pstate clock offsets (R555+, spec 17). The five deprecated
    // per-domain VF-offset symbols above stay bound as the symbol-gated
    // fallback: the only route on drivers < 555, and a one-line revert lever
    // if a future driver regresses the new semantics.
    pub get_clock_offsets_v1: Option<FnDevClockOffset>,
    pub set_clock_offsets_v1: Option<FnDevClockOffset>,
}

impl NvmlLib {
    pub(crate) fn load() -> Result<Self, GpuError> {
        // SAFETY: loading a shared library runs its initializers; the soname is
        // a trusted system library and we resolve only well-known symbols with
        // signatures matching the C ABI below.
        let lib = unsafe { Library::new("libnvidia-ml.so.1") }
            .map_err(|e| GpuError::other(format!("failed to load libnvidia-ml.so.1: {e}"), 0))?;

        Ok(Self {
            init_v2: load_req(&lib, "nvmlInit_v2")?,
            shutdown: load_req(&lib, "nvmlShutdown")?,
            handle_by_index: load_req(&lib, "nvmlDeviceGetHandleByIndex_v2")?,
            get_temperature: load_req(&lib, "nvmlDeviceGetTemperature")?,
            get_num_fans: load_req(&lib, "nvmlDeviceGetNumFans")?,
            get_fan_speed: load_req(&lib, "nvmlDeviceGetFanSpeed")?,
            get_fan_speed_v2: load_req(&lib, "nvmlDeviceGetFanSpeed_v2")?,
            get_power_usage: load_req(&lib, "nvmlDeviceGetPowerUsage")?,
            get_clock_info: load_req(&lib, "nvmlDeviceGetClockInfo")?,

            error_string: load_opt(&lib, "nvmlErrorString"),
            get_count: load_first(&lib, COUNT_SYMS),
            get_pci_info: load_first(&lib, PCI_SYMS),
            get_mem_info: load_opt(&lib, "nvmlDeviceGetMemoryInfo"),
            get_name: load_opt(&lib, "nvmlDeviceGetName"),
            get_uuid: load_opt(&lib, "nvmlDeviceGetUUID"),
            get_driver: load_opt(&lib, "nvmlSystemGetDriverVersion"),
            get_util: load_opt(&lib, "nvmlDeviceGetUtilizationRates"),
            get_throttle: load_first(&lib, THROTTLE_SYMS),
            get_minmax_fan: load_opt(&lib, "nvmlDeviceGetMinMaxFanSpeed"),

            set_fan_speed_v2: load_opt(&lib, "nvmlDeviceSetFanSpeed_v2"),
            set_default_fan: load_opt(&lib, "nvmlDeviceSetDefaultFanSpeed_v2"),
            get_pm_limit: load_opt(&lib, "nvmlDeviceGetPowerManagementLimit"),
            get_pm_default: load_opt(&lib, "nvmlDeviceGetPowerManagementDefaultLimit"),
            get_pm_constraints: load_opt(&lib, "nvmlDeviceGetPowerManagementLimitConstraints"),
            set_pm_limit: load_opt(&lib, "nvmlDeviceSetPowerManagementLimit"),
            set_persistence: load_opt(&lib, "nvmlDeviceSetPersistenceMode"),
            set_locked_clocks: load_opt(&lib, "nvmlDeviceSetGpuLockedClocks"),
            reset_locked_core: load_opt(&lib, "nvmlDeviceResetGpuLockedClocks"),
            reset_locked_mem: load_opt(&lib, "nvmlDeviceResetMemoryLockedClocks"),
            get_supported_mem_clocks: load_opt(&lib, "nvmlDeviceGetSupportedMemoryClocks"),
            get_supported_gfx_clocks: load_opt(&lib, "nvmlDeviceGetSupportedGraphicsClocks"),
            get_mem_minmax_vf: load_opt(&lib, "nvmlDeviceGetMemClkMinMaxVfOffset"),
            get_gpc_vf: load_opt(&lib, "nvmlDeviceGetGpcClkVfOffset"),
            set_gpc_vf: load_opt(&lib, "nvmlDeviceSetGpcClkVfOffset"),
            get_mem_vf: load_opt(&lib, "nvmlDeviceGetMemClkVfOffset"),
            set_mem_vf: load_opt(&lib, "nvmlDeviceSetMemClkVfOffset"),

            get_clock_offsets_v1: load_opt(&lib, "nvmlDeviceGetClockOffsets"),
            set_clock_offsets_v1: load_opt(&lib, "nvmlDeviceSetClockOffsets"),

            _lib: lib,
        })
    }
}

fn to_cstr(name: &str) -> Vec<u8> {
    let mut bytes = name.as_bytes().to_vec();
    bytes.push(0);
    bytes
}

/// Resolve a required symbol; a missing one is a hard bind error mirroring the
/// Python `AttributeError`.
fn load_req<T: Copy>(lib: &Library, name: &str) -> Result<T, GpuError> {
    // SAFETY: the caller-supplied `T` is the FFI signature declared above for
    // this exact symbol name; `Symbol` deref copies out the function pointer,
    // which stays valid while `lib` is retained in `NvmlLib`.
    unsafe {
        let sym: Symbol<T> = lib
            .get(&to_cstr(name))
            .map_err(|_| GpuError::missing_required(name))?;
        Ok(*sym)
    }
}

/// Resolve an optional symbol; `None` if absent.
fn load_opt<T: Copy>(lib: &Library, name: &str) -> Option<T> {
    // SAFETY: as `load_req`, but a missing symbol is tolerated.
    unsafe {
        let sym: Symbol<T> = lib.get(&to_cstr(name)).ok()?;
        Some(*sym)
    }
}

/// Resolve the first present symbol from a fallback chain, using the same pure
/// selection logic that the unit tests pin.
fn load_first<T: Copy>(lib: &Library, names: &[&str]) -> Option<T> {
    let name = first_available(names, |n| load_opt::<T>(lib, n).is_some())?;
    load_opt(lib, name)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// NVML packs the struct version into bits 24+ (`sizeof | ver << 24`),
    /// unlike NVAPI's `make_version` (`ver << 16`): `nvmlClockOffset_v1` is
    /// `24 | (1 << 24)`. (The 24-byte struct size is a compile-time assert.)
    #[test]
    fn clock_offset_version_matches_nvml_macro() {
        assert_eq!(nvml_struct_version(24, 1), 0x0100_0018);
        assert_eq!(NVML_CLOCK_OFFSET_V1, 16_777_240);
    }

    #[test]
    fn clock_offset_query_struct_is_tagged_for_p0() {
        let info = NvmlClockOffset::query_p0(NVML_CLOCK_MEM);
        assert_eq!(info.version, NVML_CLOCK_OFFSET_V1);
        assert_eq!(info.clock_type, NVML_CLOCK_MEM);
        assert_eq!(info.pstate, NVML_PSTATE_0);
        assert_eq!(info.clock_offset_mhz, 0);
    }
}
