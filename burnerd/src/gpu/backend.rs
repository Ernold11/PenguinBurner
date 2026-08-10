//! Real GPU backend: documented NVML through `nvml-wrapper`, with raw FFI
//! confined to the undocumented Linux NVAPI implementation in `nvapi`.

use nvml_wrapper::enum_wrappers::device::{Clock, PerformanceState, TemperatureSensor};
use nvml_wrapper::enums::device::GpuLockedClocksSetting;
use nvml_wrapper::error::NvmlError;
use nvml_wrapper::{Device, Nvml};

use super::nvapi::{NvapiVfSession, NvapiVoltageSession};
use super::{
    mw_to_w_float, mw_to_w_round, snap_core_clock_mhz, snap_core_clock_range, w_to_mw_round,
    AppliedOffsets, ClockOffsets, ClockType, GpuBackend, GpuError, GpuIdentity, GpuMemoryInfo,
    GpuResult, PowerLimits, RangeSnapResult, SnapResult, VfPoint, VfSummary,
};

pub struct NvmlBackend {
    nvml: Nvml,
    gpu_index: u32,
    voltage: Option<NvapiVoltageSession>,
    vf: Option<NvapiVfSession>,
}

impl NvmlBackend {
    pub fn open(gpu_index: u32) -> GpuResult<Self> {
        let nvml = Nvml::init().map_err(|error| runtime_error("nvmlInit_v2", error))?;
        let pci_bus_id = {
            let device = nvml
                .device_by_index(gpu_index)
                .map_err(|error| runtime_error("nvmlDeviceGetHandleByIndex_v2", error))?;
            device
                .pci_info()
                .map(|info| info.bus_id)
                .unwrap_or_default()
        };
        Ok(Self {
            nvml,
            gpu_index,
            voltage: NvapiVoltageSession::open(gpu_index, &pci_bus_id).ok(),
            vf: NvapiVfSession::open(gpu_index, &pci_bus_id).ok(),
        })
    }

    fn device(&self) -> GpuResult<Device<'_>> {
        self.nvml
            .device_by_index(self.gpu_index)
            .map_err(|error| runtime_error("nvmlDeviceGetHandleByIndex_v2", error))
    }

    fn read_clock_offset(&self, clock: Clock) -> Option<i32> {
        self.device()
            .ok()?
            .clock_offset(clock, PerformanceState::Zero)
            .ok()
            .map(|offset| offset.clock_offset_mhz)
    }

    fn apply_one_clock_offset(&self, clock: Clock, offset_mhz: i32) -> GpuResult<Option<i32>> {
        let mut device = self.device()?;
        device
            .set_clock_offset(clock, PerformanceState::Zero, offset_mhz)
            .map_err(|error| policy_error("nvmlDeviceSetClockOffsets", error))?;
        Ok(device
            .clock_offset(clock, PerformanceState::Zero)
            .ok()
            .map(|offset| offset.clock_offset_mhz))
    }
}

impl GpuBackend for NvmlBackend {
    fn gpu_index(&self) -> u32 {
        self.gpu_index
    }

    fn gpu_count(&self) -> GpuResult<u32> {
        self.nvml.device_count().map_err(|error| {
            let (code, text) = error_parts(error);
            GpuError::other(format!("NVML device count query failed: {text}"), code)
        })
    }

    fn identity(&self) -> GpuIdentity {
        let Ok(device) = self.device() else {
            return GpuIdentity {
                index: self.gpu_index,
                ..GpuIdentity::default()
            };
        };
        let pci = device.pci_info().ok();
        GpuIdentity {
            index: self.gpu_index,
            name: device.name().unwrap_or_default(),
            driver_version: self.nvml.sys_driver_version().unwrap_or_default(),
            pci_bus_id: pci
                .as_ref()
                .map(|info| info.bus_id.clone())
                .unwrap_or_default(),
            pci_device_id: pci
                .filter(|info| info.pci_device_id != 0)
                .map(|info| format!("0x{:08X}", info.pci_device_id))
                .unwrap_or_default(),
            uuid: device.uuid().unwrap_or_default(),
        }
    }

    fn gpu_name(&self) -> Option<String> {
        self.device()
            .ok()?
            .name()
            .ok()
            .filter(|name| !name.is_empty())
    }

    fn memory_info(&self) -> Option<GpuMemoryInfo> {
        let info = self.device().ok()?.memory_info().ok()?;
        Some(GpuMemoryInfo {
            index: self.gpu_index,
            total_bytes: info.total,
            free_bytes: info.free,
            used_bytes: info.used,
        })
    }

    fn architecture(&self) -> Option<u32> {
        self.device()
            .ok()?
            .architecture()
            .ok()
            .map(|architecture| architecture.as_c())
    }

    fn temperature_c(&self) -> GpuResult<f64> {
        self.device()?
            .temperature(TemperatureSensor::Gpu)
            .map(f64::from)
            .map_err(|error| runtime_error("nvmlDeviceGetTemperature", error))
    }

    fn fan_count(&self) -> GpuResult<u32> {
        self.device()?
            .num_fans()
            .map_err(|error| runtime_error("nvmlDeviceGetNumFans", error))
    }

    fn fan_speed_limits(&self) -> (Option<u32>, Option<u32>) {
        match self.device().and_then(|device| {
            device
                .min_max_fan_speed()
                .map_err(|error| runtime_error("nvmlDeviceGetMinMaxFanSpeed", error))
        }) {
            Ok((min, max)) if max >= min => (Some(min), Some(max)),
            _ => (None, None),
        }
    }

    fn reported_fan_speeds(&self, fan_count: u32) -> Option<Vec<u32>> {
        let device = self.device().ok()?;
        let speeds: Result<Vec<_>, _> = (0..fan_count)
            .map(|index| device.fan_speed(index))
            .collect();
        speeds.ok().filter(|values| !values.is_empty())
    }

    fn power_draw_w(&self) -> Option<f64> {
        self.device().ok()?.power_usage().ok().map(mw_to_w_float)
    }

    fn gpu_utilization_pct(&self) -> Option<u32> {
        self.device()
            .ok()?
            .utilization_rates()
            .ok()
            .map(|util| util.gpu)
    }

    fn clock_info_mhz(&self, clock_type: ClockType) -> Option<u32> {
        self.device()
            .ok()?
            .clock_info(wrapper_clock(clock_type))
            .ok()
    }

    fn throttle_reason_mask(&self) -> Option<u64> {
        self.device()
            .ok()?
            .current_throttle_reasons()
            .ok()
            .map(|reasons| reasons.bits())
    }

    fn gpu_context_pids(&self) -> GpuResult<Vec<u32>> {
        let device = self.device()?;
        let graphics = device
            .running_graphics_processes()
            .map_err(|error| runtime_error("nvmlDeviceGetGraphicsRunningProcesses_v3", error))?;
        let compute = device
            .running_compute_processes()
            .map_err(|error| runtime_error("nvmlDeviceGetComputeRunningProcesses_v3", error))?;
        let mut pids: Vec<u32> = graphics
            .into_iter()
            .chain(compute)
            .map(|process| process.pid)
            .collect();
        pids.sort_unstable();
        pids.dedup();
        Ok(pids)
    }

    #[allow(deprecated)]
    fn query_power_limits(&self) -> PowerLimits {
        let Ok(device) = self.device() else {
            return PowerLimits::default();
        };
        let constraints = device.power_management_limit_constraints().ok();
        PowerLimits {
            power_management_enabled: device.is_power_management_algo_active().ok(),
            power_limit_w: device.power_management_limit().ok().map(mw_to_w_round),
            enforced_power_limit_w: device.enforced_power_limit().ok().map(mw_to_w_round),
            power_limit_default_w: device
                .power_management_limit_default()
                .ok()
                .map(mw_to_w_round),
            power_limit_min_w: constraints
                .as_ref()
                .map(|limits| mw_to_w_round(limits.min_limit)),
            power_limit_max_w: constraints
                .as_ref()
                .map(|limits| mw_to_w_round(limits.max_limit)),
        }
    }

    fn apply_power_limit_w(&self, power_limit_w: i64) -> GpuResult<i64> {
        let target_mw = u32::try_from(w_to_mw_round(power_limit_w as f64))
            .map_err(|_| GpuError::other("power limit is outside the NVML range", 0))?;
        self.device()?
            .set_power_management_limit(target_mw)
            .map_err(|error| policy_error("nvmlDeviceSetPowerManagementLimit", error))?;
        Ok(power_limit_w)
    }

    fn enable_persistence_mode(&self) -> GpuResult<()> {
        self.device()?
            .set_persistent(true)
            .map_err(|error| policy_error("nvmlDeviceSetPersistenceMode", error))
    }

    fn supported_memory_clock_steps_mhz(&self) -> Vec<u32> {
        let mut steps = self
            .device()
            .ok()
            .and_then(|device| device.supported_memory_clocks().ok())
            .unwrap_or_default();
        steps.sort_unstable();
        steps.dedup();
        steps
    }

    fn supported_core_clock_steps_mhz(&self) -> Vec<u32> {
        let Ok(device) = self.device() else {
            return Vec::new();
        };
        let mut nvml_steps = Vec::new();
        for memory_clock in self.supported_memory_clock_steps_mhz() {
            if let Ok(clocks) = device.supported_graphics_clocks(memory_clock) {
                nvml_steps.extend(clocks);
            }
        }
        nvml_steps.sort_unstable();
        nvml_steps.dedup();
        nvml_steps
    }

    fn apply_locked_core_clock_mhz(
        &self,
        clock_mhz: i64,
        prefer_not_above: bool,
        snap_to_supported: bool,
    ) -> GpuResult<SnapResult> {
        let snap = if snap_to_supported {
            snap_core_clock_mhz(
                clock_mhz,
                &self.supported_core_clock_steps_mhz(),
                prefer_not_above,
            )
        } else {
            SnapResult {
                requested_clock_mhz: clock_mhz,
                applied_clock_mhz: clock_mhz,
                mode: "unsnapped".to_string(),
                supported_steps_mhz: Vec::new(),
            }
        };
        let applied = u32::try_from(snap.applied_clock_mhz)
            .map_err(|_| GpuError::other("locked core clock is outside the NVML range", 0))?;
        self.device()?
            .set_gpu_locked_clocks(GpuLockedClocksSetting::Numeric {
                min_clock_mhz: applied,
                max_clock_mhz: applied,
            })
            .map_err(|error| policy_error("nvmlDeviceSetGpuLockedClocks", error))?;
        Ok(snap)
    }

    fn apply_locked_core_clock_range_mhz(
        &self,
        min_clock_mhz: i64,
        max_clock_mhz: i64,
        prefer_max_not_above: bool,
        snap_to_supported: bool,
    ) -> GpuResult<RangeSnapResult> {
        let range = if snap_to_supported {
            snap_core_clock_range(
                min_clock_mhz,
                max_clock_mhz,
                &self.supported_core_clock_steps_mhz(),
                prefer_max_not_above,
            )
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
        let min = u32::try_from(range.applied_min_clock_mhz)
            .map_err(|_| GpuError::other("minimum core clock is outside the NVML range", 0))?;
        let max = u32::try_from(range.applied_max_clock_mhz)
            .map_err(|_| GpuError::other("maximum core clock is outside the NVML range", 0))?;
        self.device()?
            .set_gpu_locked_clocks(GpuLockedClocksSetting::Numeric {
                min_clock_mhz: min,
                max_clock_mhz: max,
            })
            .map_err(|error| policy_error("nvmlDeviceSetGpuLockedClocks", error))?;
        Ok(range)
    }

    fn reset_locked_core_clocks(&self) -> GpuResult<()> {
        self.device()?
            .reset_gpu_locked_clocks()
            .map_err(|error| policy_error("nvmlDeviceResetGpuLockedClocks", error))
    }

    fn reset_locked_memory_clocks(&self) -> GpuResult<()> {
        self.device()?
            .reset_mem_locked_clocks()
            .map_err(|error| policy_error("nvmlDeviceResetMemoryLockedClocks", error))
    }

    fn clock_offsets(&self) -> ClockOffsets {
        ClockOffsets {
            gpc_clk_vf_offset_mhz: self.read_clock_offset(Clock::Graphics),
            mem_clk_vf_offset_mhz: self.read_clock_offset(Clock::Memory),
        }
    }

    fn memory_clock_offset_range_mhz(&self) -> Option<(i32, i32)> {
        self.device()
            .ok()?
            .clock_offset(Clock::Memory, PerformanceState::Zero)
            .ok()
            .map(|offset| (offset.min_clock_offset_mhz, offset.max_clock_offset_mhz))
    }

    fn apply_clock_offsets(
        &self,
        gpc_clk_vf_offset_mhz: Option<i32>,
        mem_clk_vf_offset_mhz: Option<i32>,
    ) -> GpuResult<AppliedOffsets> {
        let mut applied = AppliedOffsets::default();
        if let Some(gpc) = gpc_clk_vf_offset_mhz {
            applied.gpc_clk_vf_offset_readback_mhz =
                self.apply_one_clock_offset(Clock::Graphics, gpc)?;
            applied.gpc_clk_vf_offset_mhz = Some(gpc);
        }
        if let Some(memory) = mem_clk_vf_offset_mhz {
            // A coarse memory write clears shaped per-point offsets, so the
            // engine's memory-before-V/F ordering remains load-bearing.
            applied.mem_clk_vf_offset_readback_mhz =
                self.apply_one_clock_offset(Clock::Memory, memory)?;
            applied.mem_clk_vf_offset_mhz = Some(memory);
        }
        Ok(applied)
    }

    fn set_fan_speed(&self, fan_idx: u32, speed_pct: u32) -> GpuResult<()> {
        self.device()?
            .set_fan_speed(fan_idx, speed_pct)
            .map_err(|error| {
                runtime_error(&format!("nvmlDeviceSetFanSpeed_v2 fan {fan_idx}"), error)
            })
    }

    fn set_all_fans_speed(&self, fan_count: u32, speed_pct: u32) -> GpuResult<()> {
        for fan_idx in 0..fan_count {
            self.set_fan_speed(fan_idx, speed_pct)?;
        }
        Ok(())
    }

    fn set_default_fan_speed(&self, fan_idx: u32) -> GpuResult<()> {
        self.device()?
            .set_default_fan_speed(fan_idx)
            .map_err(|error| {
                runtime_error(
                    &format!("nvmlDeviceSetDefaultFanSpeed_v2 fan {fan_idx}"),
                    error,
                )
            })
    }

    fn set_all_fans_default(&self, fan_count: u32) -> GpuResult<()> {
        for fan_idx in 0..fan_count {
            self.set_default_fan_speed(fan_idx)?;
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
        self.vf.as_ref().map_or_else(
            || {
                Err(GpuError::other(
                    "hidden NVAPI VF curve is not available on this system",
                    0,
                ))
            },
            NvapiVfSession::refresh_points,
        )
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
        self.vf.as_ref().map_or_else(
            || {
                Err(GpuError::other(
                    "hidden NVAPI VF curve is not available on this system",
                    0,
                ))
            },
            |vf| vf.apply_offsets_khz(offsets),
        )
    }
}

fn wrapper_clock(clock: ClockType) -> Clock {
    match clock {
        ClockType::Graphics => Clock::Graphics,
        ClockType::Sm => Clock::SM,
        ClockType::Memory => Clock::Memory,
        ClockType::Video => Clock::Video,
    }
}

fn error_parts(error: NvmlError) -> (i64, String) {
    let text = error.to_string();
    let code: u32 = error.into();
    (i64::from(code), text)
}

fn runtime_error(name: &str, error: NvmlError) -> GpuError {
    if matches!(
        &error,
        NvmlError::FailedToLoadSymbol(_) | NvmlError::FunctionNotFound
    ) {
        return GpuError::unavailable(name);
    }
    let (code, _) = error_parts(error);
    GpuError::nvml(name, code)
}

fn policy_error(name: &str, error: NvmlError) -> GpuError {
    if matches!(
        &error,
        NvmlError::FailedToLoadSymbol(_) | NvmlError::FunctionNotFound
    ) {
        return GpuError::unavailable(name);
    }
    let (code, text) = error_parts(error);
    GpuError::nvml_with_text(name, code, &text)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wrapper_errors_keep_numeric_policy_shape() {
        assert_eq!(
            policy_error("nvmlDeviceSetClockOffsets", NvmlError::NotSupported).to_string(),
            "nvmlDeviceSetClockOffsets failed with NVML error 3: the requested operation is not available on the target device"
        );
    }
}

#[cfg(test)]
mod smoke {
    use super::*;

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
        println!("architecture   = {:?}", backend.architecture());
        println!("temperature_c  = {:?}", backend.temperature_c());
        println!("fan_count      = {:?}", backend.fan_count());
        println!("fan_limits     = {:?}", backend.fan_speed_limits());
        println!("power_draw_w   = {:?}", backend.power_draw_w());
        println!("util_pct       = {:?}", backend.gpu_utilization_pct());
        println!("power_limits   = {:?}", backend.query_power_limits());
        println!("clock_offsets  = {:?}", backend.clock_offsets());
        println!("voltage_uv     = {:?}", backend.read_voltage_uv());
        println!("vf_summary     = {:?}", backend.vf_summary());
        let core_steps = backend.supported_core_clock_steps_mhz();
        println!(
            "core_steps     = {} (min={:?}, max={:?}, around_2698={:?})",
            core_steps.len(),
            core_steps.first(),
            core_steps.last(),
            snap_core_clock_mhz(2698, &core_steps, true).applied_clock_mhz,
        );
    }
}
