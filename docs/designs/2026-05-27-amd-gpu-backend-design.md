# AMD GPU Backend + Common Interface Design

**Date:** 2026-05-27
**Status:** Approved

## Summary

Add AMD GPU (RDNA2/3/4) undervolting support to PenguinBurner by introducing a thin `GpuBackend` protocol. All sweep logic, scan modes (efficiency/balanced/performance), and efficiency-stop behaviour live **once** in the existing algorithm layer and run unchanged for both vendors. The AMD backend fakes per-VF-point semantics to match the NVIDIA interface, collapsing the plan to a single `vo` offset internally when writing to the kernel sysfs interface.

---

## Hardware Constraints

| Generation | Kernel interface | Per-point control? |
|---|---|---|
| RDNA1 / RX 5000 (NV1X) | `OD_VDDC_CURVE` (3 points) | Yes |
| RDNA2 / RX 6000 (SMU13) | `vo` global mV offset | No |
| RDNA3 / RX 7000 (SMU13) | `vo` global mV offset | No |
| RDNA4 / RX 9000 (SMU14) | `vo` global mV offset | No |

Target: RDNA2, RDNA3, RDNA4. Single global `vo` offset is the only write knob. The kernel exposes the VF curve read-only (min/max SCLK, `OD_RANGE`), which gives enough information to synthesise a per-point view.

---

## Core Principle

The sweep loop (`lower_voltage_sweep_loop.py`), all scan modes, and efficiency-stop logic contain **zero vendor branches**. They operate only on:
- `base_curve: list[dict]` — VF points with `{index, voltage_mv, base_mhz, target_mhz, ...}`
- `VfCurveCandidate` — a proposed plan, carrying `voltage_mv`, `target_mhz`, `flattened_plan`

Both fields are populated correctly by the AMD backend. The per-point-to-`vo` collapse is fully encapsulated.

---

## Section 1: `GpuBackend` Protocol

**New file:** `gpu_backend.py` (project root)

```python
from typing import Protocol
from enum import Enum
from auto_uv.auto_uv_types import TelemetrySample

class VoltageControlCapability(str, Enum):
    PER_BIN = "per-bin"          # NVIDIA: per-frequency-bin offsets
    GLOBAL_OFFSET = "global-offset"  # AMD: single mV scalar

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

**NVIDIA side:** New `NvidiaGpuBackend` (thin wrapper — no changes to `HiddenNvapiVfReader` or `NvmlGpuPolicyController`).

**AMD side:** New `AmdGpuBackend` in `amd_gpu_backend.py`.

---

## Section 2: AMD Curve Representation

`AmdGpuBackend.read_base_curve()` parses `pp_od_clk_voltage` to extract `OD_SCLK` min/max and `OD_RANGE` bounds, then **synthesises N intermediate VF points** spanning the min→max clock range at equal frequency steps. N is chosen to match a typical NVIDIA bin count (~16–32 points). Each synthetic point has:

```python
{
    "index": i,
    "voltage_mv": peak_base_voltage_mv + 0,   # vo offset = 0 at baseline
    "base_mhz": interpolated_mhz,
    "target_mhz": interpolated_mhz,           # no offset at baseline
    "voltage_offset_mv": 0,
}
```

`peak_base_voltage_mv` is set to the maximum stable stock voltage read from the `OD_RANGE` upper bound. This gives the sweep loop a realistic voltage space to descend through.

---

## Section 3: Plan Building — The Single Extension Point

`LowerVoltageSweepHooks` gains one optional field:

```python
build_candidate_fn: Callable[..., VfCurveCandidate] | None = None
```

- If `None` (default): existing `build_flattened_voltage_probe_curve` is used — NVIDIA path unchanged.
- For AMD: `build_amd_vo_probe_curve` is injected. It produces a `VfCurveCandidate` where:
  - `voltage_mv` = `peak_base_voltage_mv + vo_offset_mv` (virtual target the loop reasons about)
  - `target_mhz` = peak base clock (vo doesn't affect frequency)
  - `flattened_plan` = synthetic points all with `new_offset_mhz=0` plus `voltage_offset_mv` metadata key

The sweep loop state machine, efficiency stop, acceptance logic, all scan modes — **zero lines change**.

---

## Section 4: `AmdGpuBackend.apply_plan()`

1. Read `plan[0]['voltage_offset_mv']` — the global vo in mV (e.g. `-50`)
2. Write `"manual"` → `power_dpm_force_performance_level`
3. Write `f"vo {offset}"` → `pp_od_clk_voltage`
4. Write `"c"` → `pp_od_clk_voltage` (commit)

`reset_to_defaults()`: write `"r"` → `pp_od_clk_voltage`, restore `"auto"` → `power_dpm_force_performance_level`.

Telemetry sources (`AmdGpuBackend.read_telemetry()`):
- Power: `hwmon/power1_average`
- Temperature: `hwmon/temp1_input`
- Core clock: `pp_dpm_sclk` (active level)
- Fan speed: `hwmon/fan1_input` + `hwmon/fan1_max`
- Voltage: `None` — not exposed on Linux; all callers already handle `None` gracefully

---

## Section 5: Profile Format

AMD profiles use the same YAML schema and directory as NVIDIA. Two additional fields:

```yaml
vendor: "amd"
vo_offset_mv: -50   # convenience — also derivable from flattened_plan metadata
```

All result types reused unchanged: `AutoUvVoltageScanResult`, `AutoUvProbeSummary`, `FailureKind`, `FailureSeverity`, scan mode names, efficiency/balanced/performance logic.

---

## Section 6: Factory & Auto-Detection

**New file:** `gpu_backend_factory.py`

```
1. Try NVAPI (existing path) → NvidiaGpuBackend
2. Scan /sys/class/drm/card*/device/pp_od_clk_voltage → AmdGpuBackend
3. Raise AutoUvError("no supported GPU found")
```

`initial_check/auto_uv_hardware_initial_check.py` gains an AMD branch:
- Check `pp_od_clk_voltage` exists for a DRM card
- Check `amdgpu` is the bound driver
- Check `pp_features` has OD_SCLK and voltage-overdrive bits enabled
- Emit a user-readable error if `amdgpu.ppfeaturemask` boot param is missing (required on some boards)

---

## Files Changed / Added

| File | Action |
|---|---|
| `gpu_backend.py` | New — Protocol + `VoltageControlCapability` enum |
| `amd_gpu_backend.py` | New — AMD sysfs implementation |
| `gpu_backend_factory.py` | New — auto-detection factory |
| `amd_vf_curve_builder.py` | New — `build_amd_vo_probe_curve` + curve synthesiser |
| `auto_uv/gpu/gpu_vf_curve_applier.py` | Wrap in `NvidiaGpuBackend`; expose `open_gpu_backend()` |
| `auto_uv/lower_voltage_sweep_loop.py` | Add `build_candidate_fn` to `LowerVoltageSweepHooks` |
| `auto_uv/voltage_frequency_undervolt_main_loop.py` | Use factory; inject AMD hook if needed |
| `initial_check/auto_uv_hardware_initial_check.py` | Add AMD detection branch |

Existing NVIDIA classes (`HiddenNvapiVfReader`, `NvmlGpuPolicyController`, `LiveGpuVfCurveApplier`) are **not restructured**.

---

## What Is Explicitly Out of Scope

- RDNA1 `OD_VDDC_CURVE` 3-point support
- Afterburner profile import for AMD
- AMD memory clock offset (not exposed via `pp_od_clk_voltage`)
- CUDA companion load for AMD (Q2RTX / Vulkan only)
- Windows / ROCm path
