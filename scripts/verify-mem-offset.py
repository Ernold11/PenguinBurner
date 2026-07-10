#!/usr/bin/env python3
"""Manual diagnostic for issue #20: does the memory-clock V/F offset actually land?

The production apply path (``DaemonGpuClient.apply_clock_offsets``) only
checks the NVML *return code* — it never reads the offset back, so a driver that
returns NVML_SUCCESS while silently clamping/ignoring the request is
indistinguishable from success. This script closes that gap: it applies the
value and reads it straight back with ``nvmlDeviceGetMemClkVfOffset``, and it
shows the driver's own min/max window plus PenguinBurner's app-level clamp.

Run on the affected GPU with ``penguin-burnerd`` active:

    python3 scripts/verify-mem-offset.py                 # probe only, no writes
    python3 scripts/verify-mem-offset.py --apply 1000    # apply +1000 then read back
    python3 scripts/verify-mem-offset.py --gpu 0 --apply 2000

It restores the offset to whatever it was before (or 0) on exit. This file is a
throwaway diagnostic; delete it once #20 is understood.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drivers.nvidia.daemon_gpu import DaemonGpuClient  # noqa: E402
from integrations.afterburner.policy import (  # noqa: E402
    MAX_AFTERBURNER_MEM_OFFSET_MHZ,
)


def _app_clamp(raw: int, driver_range) -> tuple[int, int]:
    """Mirror auto_uv.gpu.memory_clock_offset_user_option.auto_uv_memory_offset_mhz."""
    requested = max(0, min(MAX_AFTERBURNER_MEM_OFFSET_MHZ, int(raw)))
    effective_max = MAX_AFTERBURNER_MEM_OFFSET_MHZ
    if driver_range:
        _lo, driver_max = driver_range
        effective_max = max(0, min(MAX_AFTERBURNER_MEM_OFFSET_MHZ, int(driver_max)))
        requested = min(requested, effective_max)
    return requested, effective_max


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gpu", type=int, default=0, help="GPU index (default 0)")
    parser.add_argument(
        "--apply",
        type=int,
        default=None,
        metavar="MHZ",
        help="Offset to apply (MHz). Omit to only probe/read, without writing.",
    )
    args = parser.parse_args()

    controller = DaemonGpuClient(gpu_index=args.gpu)
    before: int | None = None
    restore_needed = False
    try:
        capabilities = controller.capabilities()
        name = capabilities.identity.name or "<unknown>"
        driver_range = None
        try:
            driver_range = capabilities.memory_clock_offset_range_mhz
        except Exception as exc:  # noqa: BLE001
            print(f"  get_memory_clock_offset_range_mhz raised: {exc}")
        before = capabilities.clock_offsets.memory_mhz

        print(f"GPU {args.gpu}: {name}")
        print(
            f"  app cap (MAX_AFTERBURNER_MEM_OFFSET_MHZ) : {MAX_AFTERBURNER_MEM_OFFSET_MHZ}"
        )
        print(
            "  driver range (nvmlDeviceGetMemClkMinMaxVfOffset): "
            + (
                f"min={driver_range[0]} max={driver_range[1]}"
                if driver_range
                else "UNSUPPORTED / None"
            )
        )
        print(f"  current mem VF offset (read back)        : {before}")
        memory_clock = controller.telemetry(refresh=True).clocks.memory_mhz
        print(f"  current MEM clock (idle unless loaded)   : {memory_clock} MHz")

        if args.apply is None:
            print(
                "\nProbe only (no --apply). What the app WOULD do for a few requests:"
            )
            for raw in (500, 1000, 2000, 3000):
                clamped, eff_max = _app_clamp(raw, driver_range)
                note = "" if clamped == raw else "  <-- CLAMPED"
                print(
                    f"    request {raw:>4} -> applies {clamped:>4} (effective_max={eff_max}){note}"
                )
            return 0

        clamped, eff_max = _app_clamp(args.apply, driver_range)
        print(
            f"\nRequested {args.apply} MHz -> app would clamp to {clamped} MHz (effective_max={eff_max})"
        )
        print(
            f"Applying RAW {args.apply} MHz via nvmlDeviceSetMemClkVfOffset (bypassing app clamp)..."
        )
        restore_needed = True
        try:
            controller.apply_clock_offsets(mem_clk_vf_offset_mhz=int(args.apply))
            print("  Set call returned NVML_SUCCESS")
        except Exception as exc:  # noqa: BLE001
            print(f"  Set call FAILED: {exc}")

        readback = controller.capabilities().clock_offsets.memory_mhz
        print(f"  read back after apply                    : {readback}")
        if readback == args.apply:
            verdict = "driver ACCEPTED the full offset"
        elif readback in (before, 0, None):
            verdict = "driver SILENTLY IGNORED it (offset unchanged)"
        else:
            verdict = f"driver CLAMPED it to {readback}"
        print(f"  verdict                                  : {verdict}")
        memory_clock = controller.telemetry(refresh=True).clocks.memory_mhz
        print(f"  MEM clock now (idle unless loaded)       : {memory_clock} MHz")

    finally:
        if restore_needed:
            restore = int(before) if before is not None else 0
            controller.apply_clock_offsets(mem_clk_vf_offset_mhz=restore)
            print(f"  restored offset to {restore}")

    print(
        "\nCAVEATS:\n"
        "  * Memory VF offset raises the TOP P-state clock only. At idle the card\n"
        "    downclocks VRAM regardless, so compare clocks UNDER a memory load\n"
        "    (e.g. the stability bench), not at the desktop.\n"
        "  * 'read back == requested' proves the driver stored it; it does NOT prove\n"
        "    the realized data rate moved. Confirm the actual GBPS under load.\n"
        "  * Whether NVML's offset is in memory-clock or transfer-rate (2x) units is\n"
        "    driver-dependent — that is why LACT +3000 may realize like ~+2000."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
