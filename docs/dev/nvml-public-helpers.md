# Public NVML helpers

PenguinBurner keeps NVIDIA driver reads and controls in `nvidia_driver/`.
These helpers bind exported NVML function names through `ctypes`; they do not
use external NVIDIA management binaries, function addresses, private query IDs,
or driver-version-specific offsets.

## Identity

`nvidia_driver.nvml_identity` provides:

- `NvmlIdentitySession.gpu_count()`
- `NvmlIdentitySession.driver_version()`
- `NvmlIdentitySession.identity(index)`
- `NvmlIdentitySession.identities()`
- `query_nvml_gpu_identity(index)`
- `query_nvml_gpu_identities()`
- `query_nvml_pci_bus_id(index)`

The identity payload includes index, GPU name, driver version, PCI bus ID, PCI
device ID, and UUID when the driver exposes them.

## Clocks

`nvidia_driver.nvml_clock` provides:

- `NvmlClockSession.current_clocks()`
- `NvmlClockSession.clock_info_mhz(clock_type)`
- `NvmlClockSession.supported_memory_clocks_mhz()`
- `NvmlClockSession.supported_graphics_clocks_mhz(memory_clock_mhz)`
- `NvmlClockSession.supported_graphics_clock_steps_mhz()`
- `query_nvml_current_clocks(index)`
- `query_nvml_supported_memory_clocks(index)`
- `query_nvml_supported_graphics_clock_steps(index)`

The current clock payload covers public NVML graphics, SM, memory, and video
clock domains. Supported-clock helpers expose memory clocks and graphics-clock
steps suitable for snapping locked-clock requests before policy application.

## Power

`nvidia_driver.nvml_power` provides:

- `NvmlPowerSession.telemetry()`
- `NvmlPowerSession.power_draw_w()`
- `NvmlPowerSession.power_management_enabled()`
- `NvmlPowerSession.power_value_w(getter_name)`
- `NvmlPowerSession.power_limit_constraints_w()`
- `query_nvml_power_telemetry(index)`
- `query_nvml_power_draw_w(index)`

The power telemetry payload includes current draw, power-management mode,
current power limit, enforced power limit, default power limit, and min/max
power-limit constraints when available.

## Policy Control

`nvidia_driver.nvml_gpu_policy` remains the mutating NVML component. It handles
persistence mode, power-limit application, locked core clocks, locked-clock
resets, supported clock lookup for snapping, and public clock VF offsets where
the installed driver exposes those symbols.

## Stability Telemetry

`stability.q2rtx.nvml_telemetry` keeps a persistent NVML handle for high-rate
stability sampling. It reads utilization, power draw, core clock, temperature,
and fan speed without launching an external query process per sample.
