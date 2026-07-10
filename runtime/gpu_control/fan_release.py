"""Release GPU fan control back to the hardware-auto controller."""

from __future__ import annotations

from runtime.daemon_client import gpu_reset_fans


def release_fans_to_hardware_auto(gpu_index: int, *, log) -> None:
    """Ask the root daemon to clear any leftover manual fan state.

    Never raises: a fan-release failure must not abort an Auto-UV scan.
    """

    try:
        gpu_reset_fans(int(gpu_index))
        log("Released fan control to the GPU hardware-auto curve for the scan.")
    except Exception as exc:  # noqa: BLE001 - never block a scan on fan release
        log(f"Warning: could not release fans to hardware-auto for the scan: {exc}")
