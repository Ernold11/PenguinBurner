//! `NvmlBackend`: the real [`GpuBackend`], tying the raw NVML library
//! ([`nvml_raw`]) to the two optional hidden-NVAPI sessions ([`nvapi`]).
//!
//! One NVML init + one long-lived device handle back the whole backend
//! (consolidating the several per-purpose Python sessions into one); the NVAPI
//! voltage and VF-curve readers are best-effort (`None` when the library or GPU
//! doesn't support them), exactly like the Python `create_hidden_*` factories.

use std::ffi::CStr;
use std::os::raw::{c_char, c_int, c_uint};

use super::nvapi::{NvapiVfSession, NvapiVoltageSession};
use super::nvml_raw::{
    Device, FnDev, FnDevIIout2, FnDevIin, FnDevIout, FnDevUUin, FnDevUUout, FnDevUUout2, FnDevUin,
    FnDevUout, NvmlLib, NvmlMemoryInfo, NvmlPciInfo, NvmlUtilization,
};
use super::{
    decode_cstr, mw_to_w_float, mw_to_w_round, snap_core_clock_mhz, snap_core_clock_range,
    w_to_mw_round, AppliedOffsets, ClockOffsets, ClockType, GpuBackend, GpuError, GpuIdentity,
    GpuMemoryInfo, GpuResult, PowerLimits, RangeSnapResult, SnapResult, VfPoint, VfSummary,
};

const NVML_TEMPERATURE_GPU: c_uint = 0;
const NVML_FEATURE_ENABLED: c_uint = 1;
#[allow(dead_code)] // identity string buffers (milestone-B identity read)
const NAME_BUFFER: usize = 96;
#[allow(dead_code)]
const UUID_BUFFER: usize = 96;
#[allow(dead_code)]
const DRIVER_BUFFER: usize = 80;
const MEM_CLOCK_CAPACITY: usize = 64;
const CORE_CLOCK_CAPACITY: usize = 512;

pub struct NvmlBackend {
    lib: NvmlLib,
    #[allow(dead_code)] // reported via identity() (milestone-B)
    gpu_index: u32,
    device: Device,
    voltage: Option<NvapiVoltageSession>,
    vf: Option<NvapiVfSession>,
    initialized: bool,
}

impl NvmlBackend {
    /// Open the backend for `gpu_index`: NVML init + device handle (fatal on
    /// failure, runtime-session error shape), then best-effort NVAPI readers.
    pub fn open(gpu_index: u32) -> GpuResult<Self> {
        let lib = NvmlLib::load()?;

        // SAFETY: `init_v2` is the resolved nvmlInit_v2.
        let rc = unsafe { (lib.init_v2)() };
        if rc != 0 {
            return Err(GpuError::nvml("nvmlInit_v2", i64::from(rc)));
        }

        let mut device: Device = std::ptr::null_mut();
        // SAFETY: valid out-pointer for the device handle.
        let rc = unsafe { (lib.handle_by_index)(gpu_index, &mut device) };
        if rc != 0 {
            // SAFETY: pairs with the successful init before we bail.
            unsafe { (lib.shutdown)() };
            return Err(GpuError::nvml(
                "nvmlDeviceGetHandleByIndex_v2",
                i64::from(rc),
            ));
        }

        let mut backend = Self {
            lib,
            gpu_index,
            device,
            voltage: None,
            vf: None,
            initialized: true,
        };

        // The hidden readers select their GPU by matching the NVML PCI bus id.
        let pci_bus_id = backend.read_pci_bus_id();
        backend.voltage = NvapiVoltageSession::open(gpu_index, &pci_bus_id).ok();
        backend.vf = NvapiVfSession::open(gpu_index, &pci_bus_id).ok();
        Ok(backend)
    }

    // --- error text (policy-controller shape only) ------------------------
    fn error_text(&self, rc: i64) -> String {
        if let Some(f) = self.lib.error_string {
            // SAFETY: nvmlErrorString returns a pointer to a static C string.
            let ptr = unsafe { f(rc as c_int) };
            if !ptr.is_null() {
                // SAFETY: `ptr` is a valid NUL-terminated string owned by NVML.
                let text = unsafe { CStr::from_ptr(ptr) }.to_string_lossy();
                if !text.is_empty() {
                    return text.into_owned();
                }
            }
        }
        format!("error={rc}")
    }

    fn policy_err(&self, name: &str, rc: i64) -> GpuError {
        let text = self.error_text(rc);
        GpuError::nvml_with_text(name, rc, &text)
    }

    // --- typed FFI call helpers (unsafe confined here) --------------------
    fn call_uout(&self, f: FnDevUout) -> (c_int, u32) {
        let mut out: c_uint = 0;
        // SAFETY: `device` is a valid handle; `out` is a valid out-pointer.
        let rc = unsafe { f(self.device, &mut out) };
        (rc, out)
    }

    fn call_uuout(&self, f: FnDevUUout, arg: c_uint) -> (c_int, u32) {
        let mut out: c_uint = 0;
        // SAFETY: `device` valid; `out` a valid out-pointer.
        let rc = unsafe { f(self.device, arg, &mut out) };
        (rc, out)
    }

    fn call_uout2(&self, f: FnDevUUout2) -> (c_int, u32, u32) {
        let (mut a, mut b): (c_uint, c_uint) = (0, 0);
        // SAFETY: `device` valid; two valid out-pointers.
        let rc = unsafe { f(self.device, &mut a, &mut b) };
        (rc, a, b)
    }

    fn call_iout(&self, f: FnDevIout) -> (c_int, i32) {
        let mut out: c_int = 0;
        // SAFETY: `device` valid; `out` a valid out-pointer.
        let rc = unsafe { f(self.device, &mut out) };
        (rc, out)
    }

    fn call_iout2(&self, f: FnDevIIout2) -> (c_int, i32, i32) {
        let (mut a, mut b): (c_int, c_int) = (0, 0);
        // SAFETY: `device` valid; two valid out-pointers.
        let rc = unsafe { f(self.device, &mut a, &mut b) };
        (rc, a, b)
    }

    fn call_uin(&self, f: FnDevUin, arg: c_uint) -> c_int {
        // SAFETY: `device` valid; scalar argument.
        unsafe { f(self.device, arg) }
    }

    fn call_uuin(&self, f: FnDevUUin, a: c_uint, b: c_uint) -> c_int {
        // SAFETY: `device` valid; scalar arguments.
        unsafe { f(self.device, a, b) }
    }

    fn call_iin(&self, f: FnDevIin, arg: c_int) -> c_int {
        // SAFETY: `device` valid; scalar argument.
        unsafe { f(self.device, arg) }
    }

    fn call_dev(&self, f: FnDev) -> c_int {
        // SAFETY: `device` valid.
        unsafe { f(self.device) }
    }

    // --- string / struct reads -------------------------------------------
    #[allow(dead_code)] // identity strings (milestone-B)
    fn read_device_string(
        &self,
        getter: Option<super::nvml_raw::FnDevCharU>,
        size: usize,
    ) -> String {
        let Some(f) = getter else {
            return String::new();
        };
        let mut buf = vec![0u8; size];
        // SAFETY: `device` valid; `buf` holds `size` bytes for the C string.
        let rc = unsafe {
            f(
                self.device,
                buf.as_mut_ptr().cast::<c_char>(),
                size as c_uint,
            )
        };
        if rc != 0 {
            return String::new();
        }
        decode_cstr(&buf)
    }

    #[allow(dead_code)] // identity string (milestone-B)
    fn driver_version(&self) -> String {
        let Some(f) = self.lib.get_driver else {
            return String::new();
        };
        let mut buf = vec![0u8; DRIVER_BUFFER];
        // SAFETY: `buf` holds DRIVER_BUFFER bytes for the C string.
        let rc = unsafe { f(buf.as_mut_ptr().cast::<c_char>(), DRIVER_BUFFER as c_uint) };
        if rc != 0 {
            return String::new();
        }
        decode_cstr(&buf)
    }

    fn read_pci_info(&self) -> Option<NvmlPciInfo> {
        let f = self.lib.get_pci_info?;
        // SAFETY: POD struct; all-zero is a valid initial value.
        let mut info: NvmlPciInfo = unsafe { std::mem::zeroed() };
        // SAFETY: `device` valid; `info` is a correctly-sized nvmlPciInfo_t.
        let rc = unsafe { f(self.device, &mut info) };
        if rc != 0 {
            return None;
        }
        Some(info)
    }

    fn read_pci_bus_id(&self) -> String {
        let Some(info) = self.read_pci_info() else {
            return String::new();
        };
        let bus_id = decode_cstr(&info.bus_id);
        if !bus_id.is_empty() {
            return bus_id;
        }
        let legacy = decode_cstr(&info.bus_id_legacy);
        if !legacy.is_empty() {
            return legacy;
        }
        format!("{:08x}:{:02x}:{:02x}.0", info.domain, info.bus, info.device)
    }

    // --- clock lists ------------------------------------------------------
    fn read_mem_clock_list(&self) -> Vec<u32> {
        let Some(f) = self.lib.get_supported_mem_clocks else {
            return Vec::new();
        };
        let mut count: c_uint = MEM_CLOCK_CAPACITY as c_uint;
        let mut values = vec![0u32; MEM_CLOCK_CAPACITY];
        // SAFETY: `device` valid; `count` preloaded with the buffer capacity;
        // `values` holds MEM_CLOCK_CAPACITY slots the driver fills.
        let rc = unsafe { f(self.device, &mut count, values.as_mut_ptr()) };
        if rc != 0 {
            return Vec::new();
        }
        values.truncate((count as usize).min(MEM_CLOCK_CAPACITY));
        values
    }

    fn read_core_clock_list(&self, memory_clock_mhz: u32) -> Vec<u32> {
        let Some(f) = self.lib.get_supported_gfx_clocks else {
            return Vec::new();
        };
        let mut count: c_uint = CORE_CLOCK_CAPACITY as c_uint;
        let mut values = vec![0u32; CORE_CLOCK_CAPACITY];
        // SAFETY: `device` valid; `count` preloaded with capacity; `values`
        // holds CORE_CLOCK_CAPACITY slots the driver fills.
        let rc = unsafe {
            f(
                self.device,
                memory_clock_mhz,
                &mut count,
                values.as_mut_ptr(),
            )
        };
        if rc != 0 {
            return Vec::new();
        }
        values.truncate((count as usize).min(CORE_CLOCK_CAPACITY));
        values
    }

    fn read_clock_offset(&self, getter: Option<FnDevIout>) -> Option<i32> {
        let f = getter?;
        let (rc, out) = self.call_iout(f);
        (rc == 0).then_some(out)
    }

    fn read_power_w_round(&self, getter: Option<FnDevUout>) -> Option<i64> {
        let f = getter?;
        let (rc, mw) = self.call_uout(f);
        (rc == 0).then(|| mw_to_w_round(mw))
    }
}

impl Drop for NvmlBackend {
    fn drop(&mut self) {
        if self.initialized {
            // SAFETY: pairs with the nvmlInit_v2 done in `open`.
            unsafe { (self.lib.shutdown)() };
            self.initialized = false;
        }
    }
}

impl GpuBackend for NvmlBackend {
    fn gpu_index(&self) -> u32 {
        self.gpu_index
    }

    fn gpu_count(&self) -> GpuResult<u32> {
        let Some(f) = self.lib.get_count else {
            return Err(GpuError::other(
                "NVML device count query is not available",
                0,
            ));
        };
        let mut count: c_uint = 0;
        // SAFETY: `count` is a valid out-pointer.
        let rc = unsafe { f(&mut count) };
        if rc != 0 {
            return Err(GpuError::other(
                format!("NVML device count query failed with error {rc}"),
                i64::from(rc),
            ));
        }
        Ok(count)
    }

    fn identity(&self) -> GpuIdentity {
        let info = self.read_pci_info();
        let pci_device_id = match &info {
            Some(i) if i.pci_device_id != 0 => format!("0x{:08X}", i.pci_device_id),
            _ => String::new(),
        };
        GpuIdentity {
            index: self.gpu_index,
            name: self.read_device_string(self.lib.get_name, NAME_BUFFER),
            driver_version: self.driver_version(),
            pci_bus_id: self.read_pci_bus_id(),
            pci_device_id,
            uuid: self.read_device_string(self.lib.get_uuid, UUID_BUFFER),
        }
    }

    fn gpu_name(&self) -> Option<String> {
        let name = self.read_device_string(self.lib.get_name, NAME_BUFFER);
        (!name.is_empty()).then_some(name)
    }

    fn memory_info(&self) -> Option<GpuMemoryInfo> {
        let f = self.lib.get_mem_info?;
        // SAFETY: POD struct; all-zero valid.
        let mut info: NvmlMemoryInfo = unsafe { std::mem::zeroed() };
        // SAFETY: `device` valid; `info` a correctly-sized nvmlMemory_t.
        let rc = unsafe { f(self.device, &mut info) };
        (rc == 0).then_some(GpuMemoryInfo {
            index: self.gpu_index,
            total_bytes: info.total,
            free_bytes: info.free,
            used_bytes: info.used,
        })
    }

    fn temperature_c(&self) -> GpuResult<f64> {
        let (rc, temp) = self.call_uuout(self.lib.get_temperature, NVML_TEMPERATURE_GPU);
        if rc != 0 {
            return Err(GpuError::nvml("nvmlDeviceGetTemperature", i64::from(rc)));
        }
        Ok(f64::from(temp))
    }

    fn fan_count(&self) -> GpuResult<u32> {
        let (rc, count) = self.call_uout(self.lib.get_num_fans);
        if rc != 0 {
            return Err(GpuError::nvml("nvmlDeviceGetNumFans", i64::from(rc)));
        }
        Ok(count)
    }

    fn fan_speed_limits(&self) -> (Option<u32>, Option<u32>) {
        let Some(f) = self.lib.get_minmax_fan else {
            return (None, None);
        };
        let (rc, min, max) = self.call_uout2(f);
        if rc == 0 && max >= min {
            (Some(min), Some(max))
        } else {
            (None, None)
        }
    }

    fn reported_fan_speeds(&self, fan_count: u32) -> Option<Vec<u32>> {
        let mut speeds = Vec::new();
        for idx in 0..fan_count {
            let (rc, speed) = self.call_uuout(self.lib.get_fan_speed_v2, idx);
            if rc != 0 {
                speeds.clear();
                break;
            }
            speeds.push(speed);
        }
        if !speeds.is_empty() {
            return Some(speeds);
        }
        if fan_count == 1 {
            let (rc, speed) = self.call_uout(self.lib.get_fan_speed);
            if rc == 0 {
                return Some(vec![speed]);
            }
        }
        None
    }

    fn power_draw_w(&self) -> Option<f64> {
        let (rc, mw) = self.call_uout(self.lib.get_power_usage);
        (rc == 0).then(|| mw_to_w_float(mw))
    }

    fn gpu_utilization_pct(&self) -> Option<u32> {
        let f = self.lib.get_util?;
        // SAFETY: POD struct; all-zero valid.
        let mut util: NvmlUtilization = unsafe { std::mem::zeroed() };
        // SAFETY: `device` valid; `util` a correctly-sized nvmlUtilization_t.
        let rc = unsafe { f(self.device, &mut util) };
        (rc == 0).then_some(util.gpu)
    }

    fn clock_info_mhz(&self, clock_type: ClockType) -> Option<u32> {
        let (rc, mhz) = self.call_uuout(self.lib.get_clock_info, clock_type.as_uint());
        (rc == 0).then_some(mhz)
    }

    fn throttle_reason_mask(&self) -> Option<u64> {
        let f = self.lib.get_throttle?;
        let mut mask: std::os::raw::c_ulonglong = 0;
        // SAFETY: `device` valid; `mask` a valid out-pointer.
        let rc = unsafe { f(self.device, &mut mask) };
        (rc == 0).then_some(mask)
    }

    fn query_power_limits(&self) -> PowerLimits {
        let (min, max) = match self.lib.get_pm_constraints {
            Some(f) => {
                let (rc, min_mw, max_mw) = self.call_uout2(f);
                if rc == 0 {
                    (Some(mw_to_w_round(min_mw)), Some(mw_to_w_round(max_mw)))
                } else {
                    (None, None)
                }
            }
            None => (None, None),
        };
        PowerLimits {
            power_limit_w: self.read_power_w_round(self.lib.get_pm_limit),
            power_limit_default_w: self.read_power_w_round(self.lib.get_pm_default),
            power_limit_min_w: min,
            power_limit_max_w: max,
        }
    }

    fn apply_power_limit_w(&self, power_limit_w: i64) -> GpuResult<i64> {
        let Some(f) = self.lib.set_pm_limit else {
            return Err(GpuError::unavailable("nvmlDeviceSetPowerManagementLimit"));
        };
        let target_mw = w_to_mw_round(power_limit_w as f64) as c_uint;
        let rc = self.call_uin(f, target_mw);
        if rc != 0 {
            return Err(self.policy_err("nvmlDeviceSetPowerManagementLimit", i64::from(rc)));
        }
        Ok(power_limit_w)
    }

    fn enable_persistence_mode(&self) -> GpuResult<()> {
        let Some(f) = self.lib.set_persistence else {
            return Err(GpuError::unavailable("nvmlDeviceSetPersistenceMode"));
        };
        let rc = self.call_uin(f, NVML_FEATURE_ENABLED);
        if rc != 0 {
            return Err(self.policy_err("nvmlDeviceSetPersistenceMode", i64::from(rc)));
        }
        Ok(())
    }

    fn supported_core_clock_steps_mhz(&self) -> Vec<u32> {
        let mut steps: Vec<u32> = Vec::new();
        for mem in self.read_mem_clock_list() {
            steps.extend(self.read_core_clock_list(mem));
        }
        steps.sort_unstable();
        steps.dedup();
        steps
    }

    fn apply_locked_core_clock_mhz(
        &self,
        clock_mhz: i64,
        prefer_not_above: bool,
        snap_to_supported: bool,
    ) -> GpuResult<SnapResult> {
        let Some(f) = self.lib.set_locked_clocks else {
            return Err(GpuError::unavailable("nvmlDeviceSetGpuLockedClocks"));
        };
        let snap = if snap_to_supported {
            let steps = self.supported_core_clock_steps_mhz();
            snap_core_clock_mhz(clock_mhz, &steps, prefer_not_above)
        } else {
            SnapResult {
                requested_clock_mhz: clock_mhz,
                applied_clock_mhz: clock_mhz,
                mode: "unsnapped".to_string(),
                supported_steps_mhz: Vec::new(),
            }
        };
        let applied = snap.applied_clock_mhz as c_uint;
        let rc = self.call_uuin(f, applied, applied);
        if rc != 0 {
            return Err(self.policy_err("nvmlDeviceSetGpuLockedClocks", i64::from(rc)));
        }
        Ok(snap)
    }

    fn apply_locked_core_clock_range_mhz(
        &self,
        min_clock_mhz: i64,
        max_clock_mhz: i64,
        prefer_max_not_above: bool,
        snap_to_supported: bool,
    ) -> GpuResult<RangeSnapResult> {
        let Some(f) = self.lib.set_locked_clocks else {
            return Err(GpuError::unavailable("nvmlDeviceSetGpuLockedClocks"));
        };
        let range = if snap_to_supported {
            let steps = self.supported_core_clock_steps_mhz();
            snap_core_clock_range(min_clock_mhz, max_clock_mhz, &steps, prefer_max_not_above)
        } else {
            RangeSnapResult {
                requested_min_clock_mhz: min_clock_mhz,
                requested_max_clock_mhz: max_clock_mhz,
                applied_min_clock_mhz: min_clock_mhz,
                applied_max_clock_mhz: max_clock_mhz,
                min_mode: "unsnapped".to_string(),
                max_mode: "unsnapped".to_string(),
                supported_steps_mhz: Vec::new(),
            }
        };
        let rc = self.call_uuin(
            f,
            range.applied_min_clock_mhz as c_uint,
            range.applied_max_clock_mhz as c_uint,
        );
        if rc != 0 {
            return Err(self.policy_err("nvmlDeviceSetGpuLockedClocks", i64::from(rc)));
        }
        Ok(range)
    }

    fn reset_locked_core_clocks(&self) -> GpuResult<()> {
        let Some(f) = self.lib.reset_locked_core else {
            return Err(GpuError::unavailable("nvmlDeviceResetGpuLockedClocks"));
        };
        let rc = self.call_dev(f);
        if rc != 0 {
            return Err(self.policy_err("nvmlDeviceResetGpuLockedClocks", i64::from(rc)));
        }
        Ok(())
    }

    fn reset_locked_memory_clocks(&self) -> GpuResult<()> {
        let Some(f) = self.lib.reset_locked_mem else {
            return Err(GpuError::unavailable("nvmlDeviceResetMemoryLockedClocks"));
        };
        let rc = self.call_dev(f);
        if rc != 0 {
            return Err(self.policy_err("nvmlDeviceResetMemoryLockedClocks", i64::from(rc)));
        }
        Ok(())
    }

    fn clock_offsets(&self) -> ClockOffsets {
        ClockOffsets {
            gpc_clk_vf_offset_mhz: self.read_clock_offset(self.lib.get_gpc_vf),
            mem_clk_vf_offset_mhz: self.read_clock_offset(self.lib.get_mem_vf),
        }
    }

    fn memory_clock_offset_range_mhz(&self) -> Option<(i32, i32)> {
        let f = self.lib.get_mem_minmax_vf?;
        let (rc, min, max) = self.call_iout2(f);
        (rc == 0).then_some((min, max))
    }

    fn apply_clock_offsets(
        &self,
        gpc_clk_vf_offset_mhz: Option<i32>,
        mem_clk_vf_offset_mhz: Option<i32>,
    ) -> GpuResult<AppliedOffsets> {
        let mut applied = AppliedOffsets::default();

        if let Some(gpc) = gpc_clk_vf_offset_mhz {
            let Some(f) = self.lib.set_gpc_vf else {
                return Err(GpuError::unavailable("nvmlDeviceSetGpcClkVfOffset"));
            };
            let rc = self.call_iin(f, gpc);
            if rc != 0 {
                return Err(self.policy_err("nvmlDeviceSetGpcClkVfOffset", i64::from(rc)));
            }
            applied.gpc_clk_vf_offset_mhz = Some(gpc);
            applied.gpc_clk_vf_offset_readback_mhz = self.read_clock_offset(self.lib.get_gpc_vf);
        }

        if let Some(mem) = mem_clk_vf_offset_mhz {
            let Some(f) = self.lib.set_mem_vf else {
                return Err(GpuError::unavailable("nvmlDeviceSetMemClkVfOffset"));
            };
            let rc = self.call_iin(f, mem);
            if rc != 0 {
                return Err(self.policy_err("nvmlDeviceSetMemClkVfOffset", i64::from(rc)));
            }
            applied.mem_clk_vf_offset_mhz = Some(mem);
            // NVML_SUCCESS does not guarantee the offset stuck (issue #20): the
            // engine compares this read-back against the request.
            applied.mem_clk_vf_offset_readback_mhz = self.read_clock_offset(self.lib.get_mem_vf);
        }

        Ok(applied)
    }

    fn set_fan_speed(&self, fan_idx: u32, speed_pct: u32) -> GpuResult<()> {
        let Some(f) = self.lib.set_fan_speed_v2 else {
            return Err(GpuError::unavailable("nvmlDeviceSetFanSpeed_v2"));
        };
        let rc = self.call_uuin(f, fan_idx, speed_pct);
        if rc != 0 {
            return Err(GpuError::nvml(
                &format!("nvmlDeviceSetFanSpeed_v2 fan {fan_idx}"),
                i64::from(rc),
            ));
        }
        Ok(())
    }

    fn set_all_fans_speed(&self, fan_count: u32, speed_pct: u32) -> GpuResult<()> {
        for idx in 0..fan_count {
            self.set_fan_speed(idx, speed_pct)?;
        }
        Ok(())
    }

    fn set_default_fan_speed(&self, fan_idx: u32) -> GpuResult<()> {
        let Some(f) = self.lib.set_default_fan else {
            return Err(GpuError::unavailable("nvmlDeviceSetDefaultFanSpeed_v2"));
        };
        let rc = self.call_uin(f, fan_idx);
        if rc != 0 {
            return Err(GpuError::nvml(
                &format!("nvmlDeviceSetDefaultFanSpeed_v2 fan {fan_idx}"),
                i64::from(rc),
            ));
        }
        Ok(())
    }

    fn set_all_fans_default(&self, fan_count: u32) -> GpuResult<()> {
        for idx in 0..fan_count {
            self.set_default_fan_speed(idx)?;
        }
        Ok(())
    }

    fn read_voltage_uv(&self) -> Option<i64> {
        self.voltage.as_ref()?.read_microvolts()
    }

    fn vf_curve_available(&self) -> bool {
        self.vf.is_some()
    }

    fn vf_points(&self) -> Vec<VfPoint> {
        self.vf
            .as_ref()
            .map(NvapiVfSession::points)
            .unwrap_or_default()
    }

    fn refresh_vf_points(&self) -> GpuResult<()> {
        match &self.vf {
            Some(vf) => vf.refresh_points(),
            None => Err(GpuError::other(
                "hidden NVAPI VF curve is not available on this system",
                0,
            )),
        }
    }

    fn editable_core_vf_points(&self) -> Vec<VfPoint> {
        self.vf
            .as_ref()
            .map(NvapiVfSession::editable_core_points)
            .unwrap_or_default()
    }

    fn find_nearest_vf_point(&self, core_clock_mhz: i64, voltage_uv: i64) -> Option<VfPoint> {
        self.vf
            .as_ref()?
            .find_nearest_point(core_clock_mhz, voltage_uv)
    }

    fn vf_summary(&self) -> VfSummary {
        self.vf
            .as_ref()
            .map(NvapiVfSession::summary)
            .unwrap_or_default()
    }

    fn apply_vf_offsets_khz(&self, offsets: &[(u32, i32)]) -> GpuResult<()> {
        match &self.vf {
            Some(vf) => vf.apply_offsets_khz(offsets),
            None => Err(GpuError::other(
                "hidden NVAPI VF curve is not available on this system",
                0,
            )),
        }
    }
}

#[cfg(test)]
mod smoke {
    use super::*;

    /// Live read-only smoke test against the real GPU. Reads identity, temp,
    /// clocks, power, voltage, VF summary and min/max offsets, and prints them.
    /// ABSOLUTELY NO write calls. Run with:
    ///   cargo test -p penguin-burnerd --lib gpu::backend::smoke -- --ignored --nocapture
    #[test]
    #[ignore = "requires a live NVIDIA GPU; read-only"]
    fn live_read_only() {
        let backend = NvmlBackend::open(0).expect("open NVML backend");
        let ident = backend.identity();
        println!("gpu_count      = {:?}", backend.gpu_count());
        println!("name           = {}", ident.name);
        println!("driver_version = {}", ident.driver_version);
        println!("pci_bus_id     = {}", ident.pci_bus_id);
        println!("pci_device_id  = {}", ident.pci_device_id);
        println!("uuid           = {}", ident.uuid);
        println!("memory_info    = {:?}", backend.memory_info());
        println!("temperature_c  = {:?}", backend.temperature_c());
        println!("fan_count      = {:?}", backend.fan_count());
        println!("fan_limits     = {:?}", backend.fan_speed_limits());
        if let Ok(n) = backend.fan_count() {
            println!("fan_speeds     = {:?}", backend.reported_fan_speeds(n));
        }
        println!("power_draw_w   = {:?}", backend.power_draw_w());
        println!("util_pct       = {:?}", backend.gpu_utilization_pct());
        println!(
            "clocks(g/s/m/v)= {:?}/{:?}/{:?}/{:?}",
            backend.clock_info_mhz(ClockType::Graphics),
            backend.clock_info_mhz(ClockType::Sm),
            backend.clock_info_mhz(ClockType::Memory),
            backend.clock_info_mhz(ClockType::Video),
        );
        println!("throttle_mask  = {:?}", backend.throttle_reason_mask());
        println!("power_limits   = {:?}", backend.query_power_limits());
        println!("clock_offsets  = {:?}", backend.clock_offsets());
        println!(
            "mem_off_range  = {:?}",
            backend.memory_clock_offset_range_mhz()
        );
        println!("voltage_uv     = {:?}", backend.read_voltage_uv());
        println!("vf_available   = {}", backend.vf_curve_available());
        println!("vf_summary     = {:?}", backend.vf_summary());
        let editable = backend.editable_core_vf_points();
        println!("vf_editable    = {}", editable.len());
        if let Some(first) = editable.first() {
            println!("vf_first_edit  = {first:?}");
        }
        let steps = backend.supported_core_clock_steps_mhz();
        println!(
            "core_steps     = {} (min={:?}, max={:?})",
            steps.len(),
            steps.first(),
            steps.last()
        );
    }
}
