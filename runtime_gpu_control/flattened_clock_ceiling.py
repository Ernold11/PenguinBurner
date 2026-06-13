"""Apply the runtime clock ceiling for a flattened V/F tail.

The controller hides exact-lock versus range-ceiling driver calls from the main loop.
"""

from __future__ import annotations

from afterburner.vfcurve_describe import describe_afterburner_dynamic_lock


class FlattenedClockCeilingController:
    def __init__(self, flatten_target, policy_controller, *, exact_lock=False):
        if not flatten_target:
            raise ValueError("flatten_target is required")
        if policy_controller is None:
            raise ValueError("policy_controller is required")

        self._flatten_target = dict(flatten_target)
        self._policy_controller = policy_controller
        self._exact_lock = bool(exact_lock)
        self._active = False
        self._range_lock = None

    @property
    def target_clock_mhz(self):
        if self._exact_lock:
            return int(self._flatten_target["lock_clock_mhz"])
        return int(
            self._flatten_target.get(
                "ceiling_clock_mhz",
                self._flatten_target["lock_clock_mhz"],
            )
        )

    @property
    def target_voltage_mv(self):
        voltage_mv = self._flatten_target.get("lock_voltage_mv")
        return int(voltage_mv) if voltage_mv is not None else None

    @property
    def requested_max_clock_mhz(self):
        if self._range_lock is None:
            return int(self.target_clock_mhz)
        return int(self._range_lock["requested_max_clock_mhz"])

    @property
    def applied_max_clock_mhz(self):
        if self._range_lock is None:
            return int(self.target_clock_mhz)
        return int(self._range_lock["applied_max_clock_mhz"])

    @property
    def applied_min_clock_mhz(self):
        if self._range_lock is None:
            return int(self.target_clock_mhz)
        return int(self._range_lock["applied_min_clock_mhz"])

    def telemetry_text(self):
        if not self._active:
            return ""

        clock_text = (
            f"{self.requested_max_clock_mhz}MHz"
            if self.requested_max_clock_mhz == self.applied_max_clock_mhz
            else f"{self.requested_max_clock_mhz}->{self.applied_max_clock_mhz}MHz"
        )
        voltage_mv = self.target_voltage_mv
        if voltage_mv is not None:
            clock_text += f"@{voltage_mv}mV"
        field = "clk_lock" if self._exact_lock else "clk_ceiling"
        return f"{field}={clock_text} "

    def describe(self):
        snap_text = ""
        if (
            self._range_lock is not None
            and self.requested_max_clock_mhz != self.applied_max_clock_mhz
        ):
            snap_text = (
                f", supported-clock={self.applied_max_clock_mhz}MHz"
                f" ({self._range_lock['max_mode']})"
            )
        min_text = (
            f", min-step={self.applied_min_clock_mhz}MHz"
            if self._range_lock is not None and not self._exact_lock
            else ""
        )
        policy_text = "lock" if self._exact_lock else "ceiling"
        return (
            f"{describe_afterburner_dynamic_lock(self._flatten_target)}, "
            f"{policy_text}={self.requested_max_clock_mhz}MHz"
            f"{snap_text}{min_text}"
        )

    def apply(self):
        if self._exact_lock:
            snap = self._policy_controller.apply_locked_core_clock_mhz(
                self.target_clock_mhz,
                prefer_not_above=True,
                snap_to_supported=True,
            )
            applied_clock_mhz = int(snap["applied_clock_mhz"])
            self._range_lock = {
                "requested_min_clock_mhz": int(self.target_clock_mhz),
                "requested_max_clock_mhz": int(self.target_clock_mhz),
                "applied_min_clock_mhz": applied_clock_mhz,
                "applied_max_clock_mhz": applied_clock_mhz,
                "min_mode": str(snap["mode"]),
                "max_mode": str(snap["mode"]),
                "supported_steps_mhz": list(snap.get("supported_steps_mhz") or []),
            }
            self._active = True
            return dict(self._range_lock)

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

    def close(self):
        if self._active:
            self._policy_controller.reset_locked_core_clocks()
            self._active = False

    def retarget(self, flatten_target):
        self.close()
        self._flatten_target = dict(flatten_target)
        return self.apply()
