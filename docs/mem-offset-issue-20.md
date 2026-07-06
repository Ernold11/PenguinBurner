# Memory overclock not applying (issue #20)

> **Status 2026-07-01: verified WORKING on Blackwell (RTX 5080, see results
> below) — the apply path is not broken.** Reported against **RTX 6000
> (Blackwell)** by @xinanhuang
> ([#20](https://github.com/jpietek/PenguinBurner/issues/20)): a `+2000 MHz`
> memory offset yields no memory-clock increase, while **LACT `+3000 MHz`
> applies** (verified ~32.6 GBPS, no ECC errors). Ships with
> `scripts/verify-mem-offset.py` to confirm on the Blackwell box whether an
> offset actually lands.

## Hardware verification (RTX 5080, 2026-07-01)

Ran on a Blackwell RTX 5080 (driver-reported offset window `-2000..+6000`),
under a 100% `stability.cuda_bruteforce` load, sampling
`nvidia-smi --query-gpu=clocks.mem`:

| Applied via `nvmlDeviceSetMemClkVfOffset` | Read-back | Loaded mem clock |
| --- | --- | --- |
| 0 | 0 | 14801 MHz (P1) |
| +1000 | +1000 — ACCEPTED | 15301 MHz (**+500**) |
| +2000 (app cap) | +2000 — ACCEPTED | 15801 MHz (**+1000**) |
| +3000 raw (bypasses app cap) | +3000 — ACCEPTED | not tested under load |

Conclusions:

- **The apply path works on Blackwell.** The driver stores the offset AND the
  realized clock moves under load. `apply_clock_offsets()` is not silently
  failing on this generation/driver.
- **The NVML offset is in transfer-rate (MT/s) units**: the realized
  memory-clock delta is exactly half the requested offset. So the reporter's
  LACT `+3000` ≈ `+1500` in `nvidia-smi` clock terms, and PB's `+2000` cap
  ≈ `+1000` — the unit-ambiguity caveat below is resolved (2× on Blackwell).
- **Idle observation shows nothing**, as predicted: at the desktop the VRAM
  sits at 405 MHz (P8) regardless of offset. A transient/light 3D load
  (vkcube) never held the top mem P-state either — the reporter comparing
  clocks outside a sustained load would see "no increase" from a working
  offset.
- Most likely explanation for #20: observation at idle/light load, plus the
  hard `2000` cap making PB's ceiling half of what LACT was asked for.
  Remaining unknown: whether the RTX 6000 (workstation) driver behaves
  differently — the reporter running `scripts/verify-mem-offset.py` on that
  box would settle it.

## TL;DR

Two separate things are in play, and only hardware can tell them apart:

1. **A hard app cap makes 2000–3000 unrequestable.** Every entry path clamps
   the memory offset to `0..2000` MHz, so the reporter's working LACT `+3000`
   *cannot even be expressed* in PenguinBurner, and `2000` is the ceiling.
2. **The apply is unverified.** `apply_clock_offsets()` only checks the NVML
   return code and **never reads the offset back**, so a driver that returns
   `NVML_SUCCESS` while silently clamping/ignoring the request looks identical
   to success. The clamp is also silent (no log line), so requested-vs-applied
   is invisible in normal runs.

## Where the memory offset is applied

Every path funnels into `NvmlGpuPolicyController.apply_clock_offsets()`, which
calls `nvmlDeviceSetMemClkVfOffset` — `drivers/nvidia/nvml_gpu_policy.py:521`.
It raises on a non-`SUCCESS` return code and otherwise assumes success. It does
**not** call `nvmlDeviceGetMemClkVfOffset` afterwards to confirm the value
stuck.

Callers:

| Path | Site | Clamp applied |
| --- | --- | --- |
| Auto-UV live run | `auto_uv/gpu/gpu_vf_curve_applier.py:110` | app cap **and** driver range |
| Saved profile apply | `profiles/uv/runtime_auto_uv_profile.py:179` | app cap only |
| VF-curve reset re-apply | `runtime/fan_control/runtime_loop.py:726` | re-applies the user's value (does **not** zero it) |
| Afterburner import | `integrations/afterburner/policy.py:210` | app cap (`-2000..2000`) |

## The two clamps

1. **App hard cap — `MAX_AFTERBURNER_MEM_OFFSET_MHZ = 2000`**
   (`integrations/afterburner/policy.py:10`). Applied on *every* entry path:
   CLI (`cli/arguments.py:132`), Auto-UV option
   (`auto_uv/gpu/memory_clock_offset_user_option.py:23`), saved profile
   (`profiles/uv/runtime_auto_uv_profile.py:118`), effective runtime options
   (`cli/effective_runtime_options.py:19`). Anything `>= 2000` becomes exactly
   `2000`.
2. **Driver-range clamp (Auto-UV path only)** — additionally clamps to whatever
   `nvmlDeviceGetMemClkMinMaxVfOffset` reports as the max
   (`auto_uv/gpu/memory_clock_offset_user_option.py:30-33`). The saved-profile
   path skips this, so the two paths can apply different values for the same
   request.

## Answering the report

- **"Is it just a threshold that 2000–3000 isn't possible?"** — Yes, by design.
  `>= 2000` is capped to `2000`, so LACT's `+3000` is unrequestable and `2000`
  is the ceiling. On the Auto-UV path the driver-range clamp can pull it lower
  still.
- **"Did a `+1000` offset actually apply?"** — Unknowable from the app today.
  `1000` is under the cap so the app cap leaves it alone, but the driver-range
  clamp could reduce it (Auto-UV path) or the driver could accept-then-ignore
  it. Nothing reads it back and nothing logs the applied value (unlike power
  limit, which logs at `gpu_vf_curve_applier.py:98`).

## Verifying on hardware

`scripts/verify-mem-offset.py` closes the read-back gap. Run it **on the
Blackwell GPU** (needs `libnvidia-ml` + write perms):

```sh
sudo python3 scripts/verify-mem-offset.py             # probe: driver range + what the app would clamp
sudo python3 scripts/verify-mem-offset.py --apply 2000
sudo python3 scripts/verify-mem-offset.py --apply 3000  # raw, bypasses the 2000 app cap
```

It prints the driver's min/max window, applies the raw value, **reads it back**,
and classifies the result as *accepted / silently-ignored / clamped-to-N*. It
restores the prior offset on exit.

### Read the results with these caveats

- **Idle vs. load.** Memory VF offset lifts the **top P-state** clock only; at
  idle the VRAM downclocks regardless. Comparing idle `nvidia-smi` memory clocks
  will show no change even on a working offset — observe under a memory load
  (the stability bench). This alone could explain the report.
- **Stored ≠ realized.** "Read back == requested" proves the driver stored the
  offset; it does **not** prove the data rate moved. Confirm actual GBPS under
  load.
- **Unit ambiguity.** Whether NVML's offset is in memory-clock or transfer-rate
  (2×) units is driver-dependent — a plausible reason LACT `+3000` realizes like
  ~`+2000`.

## Fixes — IMPLEMENTED 2026-07-02

1. **Read-back + logging — DONE.** `apply_clock_offsets` now reads every offset
   back after the set and returns it as `*_readback_mhz`. The Auto-UV apply
   site logs `applied +N MHz, NVML read-back confirms +N MHz`, a loud
   `MISMATCH` line when the driver clamped/ignored the request, and the app
   clamp itself (`requested X clamped to Y (limit Z)`). The runtime VF-reset
   re-apply logs `event=mem-offset-reapplied requested=N readback=N`.
2. **Static `2000` cap lifted — DONE.** The driver-reported max from
   `nvmlDeviceGetMemClkMinMaxVfOffset` (via the new
   `driver_memory_offset_limit_mhz()`) is now the clamp authority on every
   path — Auto-UV option, saved-profile apply, Auto-UV dialog spinbox range —
   with `MAX_AFTERBURNER_MEM_OFFSET_MHZ = 2000` kept only as the fallback when
   NVML exposes no range (and for Afterburner import translation). The CLI no
   longer statically clamps; the apply path clamps and logs. On the 5080 the
   dialog now offers 0..6000, so the reporter's `+3000` is expressible.
3. **Units surfaced in the UI.** The dialog spinbox is labeled MT/s with a
   live `= +N MHz memory clock` conversion (half the offset, per the 2×
   transfer-rate unit confirmed above), matching the Afterburner/LACT slider
   convention.
