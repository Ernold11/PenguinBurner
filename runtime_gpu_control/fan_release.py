"""Release GPU fan control back to the hardware-auto controller.

Used at Auto-UV scan start: the probe drives clocks/voltage erratically, so
PenguinBurner deliberately does not run its own fan control during a scan.
Curve-based fan control would be meaningless there and could fight the probe.
"""

from __future__ import annotations

import os

from .nvml_runtime_session import NvmlRuntimeSession


def release_fans_to_hardware_auto(gpu_index: int, *, log) -> None:
    """Hand fan control back to the GPU's own hardware-auto fan curve.

    Clears any leftover manual fan state from a previously applied profile so the
    GPU governs cooling for the duration of the scan. Never raises: a fan-release
    failure must not abort the scan.
    """

    if os.geteuid() != 0:
        return
    session = None
    try:
        session = NvmlRuntimeSession(int(gpu_index))
        session.set_all_fans_default(session.fan_count())
        log("Released fan control to the GPU hardware-auto curve for the scan.")
    except Exception as exc:  # noqa: BLE001 - never block a scan on fan release
        log(f"Warning: could not release fans to hardware-auto for the scan: {exc}")
    finally:
        if session is not None:
            session.close()
