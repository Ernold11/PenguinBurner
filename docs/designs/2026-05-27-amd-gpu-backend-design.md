# AMD GPU Backend + Common Interface Design

**Date:** 2026-05-27
**Status:** Approved (revised after review)

## Summary

Add AMD GPU (RDNA2/3/4) undervolting support to PenguinBurner by introducing a thin `GpuBackend` protocol. All sweep logic, scan modes (efficiency/balanced/performance), and efficiency-stop behaviour live **once** in the existing algorithm layer and run unchanged for both vendors. The AMD backend fakes per-VF-point semantics at the protocol boundary, collapsing each candidate to a single `vo` offset internally when writing to the kernel sysfs interface.

---

## Hardware Constraints (verified)

| Generation | Kernel readback token | Write knobs |
|---|---|---|
| RDNA1 / RX 5000 (NV1X) | `OD_VDDC_CURVE` (3 points) | `vc index clk mv` per-point |
| RDNA2 / RX 6000 (SMU13) | `OD_VDDGFX_OFFSET: NmV` | single `vo N` global mV |
| RDNA3 / RX 7000 (SMU13) | `OD_VDDGFX_OFFSET: NmV` | single `vo N` global mV |
| RDNA4 / RX 9000 (SMU14) | `OD_VDDGFX_OFFSET: NmV` | single `vo N` global mV |

Confirmed against LACT real-hardware snapshots (RX 5700 XT through RX 9070 XT) and against `smu_v13_0_0_ppt.c`, `smu_v13_0_7_ppt.c`, `smu_v14_0_2_ppt.c`. The kernel literally writes a single user-supplied scalar into all 6 zones of `VoltageOffsetPerZoneBoundary[]`. The firmware struct supports per-zone offsets and (RDNA4) Advanced OD / FullCtrl modes, but no kernel sysfs/debugfs/IOCTL drives them. **`pp_table` runtime upload is broken on RDNA3/4** (driver does not fully apply; 4095-byte truncation bug per `upp`), so editing the PowerPlay table directly is not a viable workaround.

**Target: RDNA2, RDNA3, RDNA4.** Single global `vo` offset is the only write knob. RDNA1 is explicitly out of scope (different code path).

Reference for our own approach: LACT (`lact-daemon/src/server/gpu_controller/amd.rs`) and PenguinBurner's existing `lact/export.py` (`_vf_curve_yaml_from_points`) already correctly produce per-point `gpu_vf_curve:` only for NVIDIA and a scalar `voltage_offset:` for AMD.

---

## Core Principle

The sweep loop (`lower_voltage_sweep_loop.py`), all scan modes, and efficiency-stop logic contain **zero vendor branches**. They operate only on:
- `base_curve: list[dict]` — VF points with `{index, voltage_mv, base_mhz, target_mhz, ...}`
- `VfCurveCandidate` — a proposed plan, carrying `voltage_mv`, `target_mhz`, `flattened_plan`

Both fields are populated correctly by the AMD backend. The per-point-to-`vo` collapse is fully encapsulated in the AMD backend and the AMD candidate builder. Vendor selection happens **once**, at the entry point of the auto-UV main loop, where the right `build_candidate_fn` is injected into the sweep hooks.

---

## Section 1: `GpuBackend` Protocol

**New file:** `gpu_backend.py` (project root)

```python
from typing import Protocol
from enum import Enum
from auto_uv.auto_uv_types import TelemetrySample

class VoltageControlCapability(str, Enum):
    PER_BIN = "per-bin"          # NVIDIA: per-frequency-bin offsets
    GLOBAL_OFFSET = "global-offset"  # AMD RDNA2+: single mV scalar

class GpuBackend(Protocol):
    @property
    def voltage_control_capability(self) -> VoltageControlCapability: ...

    def read_base_curve(self) -> list[dict]: ...
    def apply_plan(self, plan: list[dict]) -> None: ...
    def reset_to_defaults(self) -> None: ...
    def read_telemetry(self) -> TelemetrySample: ...
    def set_power_limit(self, watts: int) -> None: ...
    def get_power_limits(self) -> dict: ...
    def apply_clock_offsets(self, *, mem_clk_vf_offset_mhz: int) -> None: ...
    def close(self) -> None: ...
```

**Role of `voltage_control_capability`:** purely informational — used by the main-loop entry point to pick the right candidate builder, and by the UI / profile metadata for labelling. The sweep loop itself does not branch on it.

**NVIDIA side:** New `NvidiaGpuBackend` (thin wrapper — no changes to `HiddenNvapiVfReader` or `NvmlGpuPolicyController`).

**AMD side:** New `AmdGpuBackend` in `amd_gpu_backend.py`.

---

## Section 2: AMD Curve Representation (revised)

The kernel cannot vary frequency per voltage point on RDNA2+ — `vo` shifts the *whole* curve uniformly. So the synthesised `base_curve` represents **N candidate target voltages at a fixed peak frequency**, not N frequencies. The sweep loop steps *down* through `base_curve` looking for a lower stable voltage; that is exactly what AMD needs.

`AmdGpuBackend.read_base_curve()` does:

1. Parse `pp_od_clk_voltage` to extract `OD_SCLK` max and `OD_RANGE` SCLK / VDDGFX_OFFSET ranges.
2. Treat the upper end of the kernel-reported voltage envelope as `peak_base_voltage_mv` (a virtual stock-Vmax anchor — the actual physical Vmax is firmware-internal; we only need a fixed anchor so `voltage_mv = anchor + vo` is reversible).
3. Read the current `OD_VDDGFX_OFFSET` and treat it as the starting offset.
4. Synthesise N points (default 16) spanning a configurable mV window — e.g. `peak_base_voltage_mv` down to `peak_base_voltage_mv + min_allowed_offset_mv` from `OD_RANGE`:

```python
{
    "index": i,
    "voltage_mv": peak_base_voltage_mv + step_offsets_mv[i],   # varies per point
    "base_mhz": peak_sclk_mhz,                                  # constant
    "target_mhz": peak_sclk_mhz,                                # constant; vo doesn't move freq
    "voltage_offset_mv": step_offsets_mv[i],                    # AMD-specific metadata
    "preserve_base": False,
    "original": {"vendor": "amd", "synthesised": True},
}
```

The sweep loop steps `voltage_mv` downward through these synthetic points. The AMD candidate builder converts each chosen `voltage_mv` back into a `vo` offset by subtracting `peak_base_voltage_mv`.

Step granularity (e.g. 5 mV or 10 mV) is a tunable in `auto_uv_scan_settings` and bounded below by `OD_RANGE`'s minimum allowed VDDGFX_OFFSET.

---

## Section 3: Plan Building — The Single Extension Point

`LowerVoltageSweepHooks` gains one optional field:

```python
build_candidate_fn: Callable[..., VfCurveCandidate] | None = None
```

- If `None` (default): existing `build_flattened_voltage_probe_curve` is used — NVIDIA path unchanged.
- For AMD: `build_amd_vo_probe_curve` is injected. It produces a `VfCurveCandidate` where:
  - `voltage_mv` = the candidate voltage chosen by the loop (matches a synthetic point's `voltage_mv`)
  - `target_mhz` = peak base clock (vo doesn't affect frequency)
  - `flattened_plan` = the synthetic points, with the metadata key `voltage_offset_mv = candidate_voltage_mv - peak_base_voltage_mv`

**Dispatch point (explicit):** in `auto_uv/voltage_frequency_undervolt_main_loop.py`, after opening the backend via the factory:

```python
backend = open_gpu_backend(gpu_index=...)
if backend.voltage_control_capability is VoltageControlCapability.GLOBAL_OFFSET:
    build_candidate_fn = build_amd_vo_probe_curve
else:
    build_candidate_fn = None  # default NVIDIA per-bin builder
hooks = LowerVoltageSweepHooks(..., build_candidate_fn=build_candidate_fn)
```

This is the **only** place vendor branches. The sweep loop, scan modes, efficiency-stop logic, and acceptance code change zero lines.

---

## Section 4: `AmdGpuBackend.apply_plan()` and `reset_to_defaults()`

**Apply:**
1. Read `plan[0]['voltage_offset_mv']` — the global `vo` in mV (e.g. `-50`).
2. Write `"manual"` → `power_dpm_force_performance_level`.
3. Write `f"vo {offset}"` → `pp_od_clk_voltage`.
4. Write `"c"` → `pp_od_clk_voltage` (commit).

**Reset:**
1. Ensure `power_dpm_force_performance_level` is `"manual"` (required for any pp_od write).
2. Write `"r"` → `pp_od_clk_voltage` (restore).
3. Write `"c"` → `pp_od_clk_voltage` (commit the restore).
4. Write `"auto"` → `power_dpm_force_performance_level`.

Telemetry sources (`AmdGpuBackend.read_telemetry()`):
- Power: `hwmon/power1_average` (microwatts → W).
- Temperature: `hwmon/temp1_input` (millicelsius → °C; pick `temp1` (edge) — `temp2` (junction) and `temp3` (memory) are also exposed, edge matches NVIDIA semantics).
- Core clock: parse `pp_dpm_sclk` and pick the line marked `*` (active DPM level), e.g. `"7: 2495Mhz *"`.
- Fan speed: `hwmon/fan1_input` and `hwmon/fan1_max` to compute pct.
- Voltage: `None`. Linux does not expose instantaneous load voltage on RDNA2+ (`AMDGPU_INFO_SENSOR_VDDGFX` exists in the IOCTL header but driver returns the configured rail voltage on most ASICs, not the loaded value). All callers in the codebase already handle `None` voltage gracefully.

---

## Section 5: Privileges and Permissions

Writes to `pp_od_clk_voltage`, `power_dpm_force_performance_level`, and `hwmon/*_cap` files require `CAP_DAC_OVERRIDE` / `CAP_SYS_ADMIN` (effectively root). Behaviour:

- **Daemon path (systemd unit, already root):** writes go through directly.
- **Interactive CLI path:** `AmdGpuBackend.__init__` opens the sysfs files for write at construction. If `EACCES`, raise `AutoUvError` with a clear message instructing the user to either run via the systemd daemon or relax permissions on the relevant files (matching how the NVIDIA path handles `nvidia-smi -pm` requirements today).
- **Initial-check** prints the required write paths so users can grant `chmod g+w` + group ownership if they prefer that to running root.

The existing NVIDIA pipeline already deals with this in `nvidia_runtime_defaults.py`; the AMD path follows the same UX shape.

---

## Section 6: Profile Format

AMD profiles use the same YAML schema and directory as NVIDIA. Two additional fields:

```yaml
vendor: "amd"
vo_offset_mv: -50   # convenience — also derivable from flattened_plan metadata
peak_base_voltage_mv: 1150   # the anchor used at scan time (so reapply is self-consistent)
```

All result types reused unchanged: `AutoUvVoltageScanResult`, `AutoUvProbeSummary`, `FailureKind`, `FailureSeverity`, scan mode names (efficiency / balanced / performance), efficiency-stop policy, acceptance logic. LACT export reuses the existing `voltage_offset:` scalar path for AMD profiles.

---

## Section 7: Factory & Auto-Detection (revised)

**New file:** `gpu_backend_factory.py`

```
1. Try NVAPI (existing path) → NvidiaGpuBackend
2. Scan /sys/class/drm/card*/device/ for AMD candidates:
   a. driver bound is `amdgpu`
   b. read pp_od_clk_voltage; require literal token `OD_VDDGFX_OFFSET` in output
      (this rejects RDNA1 NV1X cards which expose OD_VDDC_CURVE only)
   c. require non-empty `OD_RANGE` with VDDGFX_OFFSET row
   → AmdGpuBackend(card_path)
3. Raise AutoUvError("no supported GPU backend found") with diagnostics
```

`initial_check/auto_uv_hardware_initial_check.py` gains an AMD branch:
- Confirm `pp_od_clk_voltage` exists for a DRM card and `amdgpu` is the bound driver.
- Confirm readback contains `OD_VDDGFX_OFFSET` (RDNA2+ marker; rejects RDNA1).
- Confirm `pp_features` has the OD voltage-offset bit set; if not, surface a message about `amdgpu.ppfeaturemask` boot parameter (some boards default it off).
- Verify write permission on `pp_od_clk_voltage` and `power_dpm_force_performance_level`; emit clear remediation if not.

---

## Files Changed / Added

| File | Action |
|---|---|
| `gpu_backend.py` | New — Protocol + `VoltageControlCapability` enum |
| `nvidia_gpu_backend.py` | New — thin wrapper around existing NVAPI/NVML helpers |
| `amd_gpu_backend.py` | New — sysfs implementation for RDNA2+ |
| `gpu_backend_factory.py` | New — `open_gpu_backend()` auto-detection |
| `amd_vf_curve_builder.py` | New — `build_amd_vo_probe_curve` + base curve synthesiser |
| `auto_uv/lower_voltage_sweep_loop.py` | Add `build_candidate_fn` to `LowerVoltageSweepHooks` |
| `auto_uv/voltage_frequency_undervolt_main_loop.py` | Open backend via factory; inject builder per capability |
| `auto_uv/gpu/gpu_vf_curve_applier.py` | Expose backend handle (no internal restructure) |
| `initial_check/auto_uv_hardware_initial_check.py` | Add AMD detection + permissions checks |

Existing NVIDIA classes (`HiddenNvapiVfReader`, `NvmlGpuPolicyController`, `LiveGpuVfCurveApplier`, all of `afterburner/`, `runtime_gpu_control/`) are **not restructured**.

---

## Out of Scope

- RDNA1 `OD_VDDC_CURVE` 3-point support (different code path; revisit later if requested).
- Afterburner profile import for AMD (vendor-specific, no equivalent on AMD side).
- AMD memory clock offset semantics — `pp_od_clk_voltage` exposes `m 0/1` absolute min/max only, not an offset; not mappable to NVIDIA's `mem_clk_vf_offset_mhz` model. Out of scope for v1.
- CUDA companion load for AMD (Q2RTX / Vulkan only — already cross-vendor).
- Per-zone voltage offsets on RDNA3/4 — firmware supports it, kernel does not expose it. Would require kernel patches.
- `pp_table` SoftPowerPlay editing — broken on RDNA3/4 per upstream tooling.
- Windows / ROCm path.
