"""Apply the temporary core-clock ceiling used while probing flat curves.

The ceiling keeps the driver from boosting past the flattened target clock.
"""

from __future__ import annotations

from integrations.afterburner.vfcurve_describe import describe_afterburner_dynamic_lock
from nvidia_driver.nvml_gpu_policy import NvmlGpuPolicyController


class ProbeClockCeilingController:
    def __init__(
        self,
        flatten_target: dict,
        policy_controller: NvmlGpuPolicyController,
    ):
        self._flatten_target = dict(flatten_target)
        self._policy_controller = policy_controller
        self._range_lock: dict | None = None
        self._active = False

    @property
    def target_clock_mhz(self) -> int:
        return int(
            self._flatten_target.get(
                "ceiling_clock_mhz",
                self._flatten_target["lock_clock_mhz"],
            )
        )

    @property
    def target_voltage_mv(self) -> int | None:
        value = self._flatten_target.get("lock_voltage_mv")
        return int(value) if value is not None else None

    def apply(self) -> dict:
        supported_steps = self._policy_controller.get_supported_core_clock_steps_mhz()
        requested_min_clock_mhz = (
            supported_steps[0] if supported_steps else self.target_clock_mhz
        )
        self._range_lock = self._policy_controller.apply_locked_core_clock_range_mhz(
            requested_min_clock_mhz,
            self.target_clock_mhz,
            prefer_max_not_above=True,
            snap_to_supported=True,
        )
        self._active = True
        return dict(self._range_lock)

    def retarget(
        self,
        *,
        lock_clock_mhz: int,
        lock_voltage_mv: int | None = None,
        ceiling_clock_mhz: int | None = None,
    ) -> dict:
        self._flatten_target["lock_clock_mhz"] = int(lock_clock_mhz)
        if lock_voltage_mv is not None:
            self._flatten_target["lock_voltage_mv"] = int(lock_voltage_mv)
        if ceiling_clock_mhz is not None and int(ceiling_clock_mhz) > int(lock_clock_mhz):
            self._flatten_target["ceiling_clock_mhz"] = int(ceiling_clock_mhz)
        else:
            self._flatten_target.pop("ceiling_clock_mhz", None)
        if self._active:
            self._policy_controller.reset_locked_core_clocks()
            self._active = False
        return self.apply()

    def describe(self) -> str:
        if self._range_lock is None:
            return describe_afterburner_dynamic_lock(self._flatten_target)
        requested_max = int(self._range_lock["requested_max_clock_mhz"])
        applied_max = int(self._range_lock["applied_max_clock_mhz"])
        voltage_mv = self.target_voltage_mv
        ceiling_text = (
            f"{requested_max}MHz"
            if requested_max == applied_max
            else f"{requested_max}->{applied_max}MHz"
        )
        if voltage_mv is not None:
            ceiling_text += f"@{voltage_mv}mV"
        return (
            f"{describe_afterburner_dynamic_lock(self._flatten_target)}, "
            f"ceiling={ceiling_text}"
        )

    def close(self) -> None:
        if not self._active:
            return
        self._policy_controller.reset_locked_core_clocks()
        self._active = False
