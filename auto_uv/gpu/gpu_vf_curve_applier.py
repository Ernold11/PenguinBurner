"""Apply Auto-UV V/F curve plans to the live GPU.

This is the only Auto-UV module that creates NVAPI/NVML helpers and applies curve plans.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, cast

from drivers.nvidia.daemon_gpu import DaemonGpuClient
from runtime.support.vf_curve_plan import apply_plan
from runtime.support.nvidia_runtime_defaults import reset_nvidia_runtime_defaults

from auto_uv.domain.types import AutoUvError
from .memory_clock_offset_user_option import auto_uv_memory_offset_mhz
from .probe_clock_ceiling import ProbeClockCeilingController
from .runtime_vf_offset_reset_check import assert_zero_runtime_vf_offsets


@dataclass(slots=True)
class LiveGpuVfCurveApplier:
    gpu_index: int
    gpu: DaemonGpuClient
    runtime_default_plan: list[dict]
    translated_gpu_policy: dict
    baseline_power_limit_w: int | None = None
    requested_power_limit_w: int | None = None
    min_power_limit_w: int | None = None
    max_power_limit_w: int | None = None
    clock_ceiling: ProbeClockCeilingController | None = None

    @property
    def reader(self) -> DaemonGpuClient:
        return self.gpu

    @property
    def policy_controller(self) -> DaemonGpuClient:
        return self.gpu

    @property
    def live_voltage_reader(self) -> DaemonGpuClient:
        return self.gpu

    @property
    def power_limit_w(self) -> int | None:
        value = self.translated_gpu_policy.get("power_limit_w")
        return int(value) if value is not None else None

    def apply_plan(self, plan: list[dict]) -> None:
        apply_plan(self.reader, plan)
        refresh_points = getattr(self.reader, "refresh_points", None)
        if callable(refresh_points):
            refresh_points()

    def start_clock_ceiling(self, flatten_target: dict) -> None:
        self.clock_ceiling = ProbeClockCeilingController(
            flatten_target=flatten_target,
            policy_controller=self.policy_controller,
        )
        self.clock_ceiling.apply()

    def clamp_power_limit_w(self, watts: int) -> int:
        """Keep a requested board-power cap inside the card's [min, max].

        The NVML driver rejects a limit below the hardware minimum (e.g. a
        5080's 300W floor), so an efficiency tier's computed cap must clamp
        up rather than fail the apply."""
        clamped = int(watts)
        if self.min_power_limit_w is not None:
            clamped = max(clamped, int(self.min_power_limit_w))
        if self.max_power_limit_w is not None:
            clamped = min(clamped, int(self.max_power_limit_w))
        return clamped

    def apply_requested_power_limit(self, *, log: Callable[[str], None]) -> int | None:
        requested_w = _positive_power_limit_w(self.requested_power_limit_w)
        if requested_w is None:
            return self.power_limit_w
        requested_w = self.clamp_power_limit_w(int(requested_w))
        if self.power_limit_w == requested_w:
            self.requested_power_limit_w = None
            return self.power_limit_w
        try:
            applied_power_limit_w = self.policy_controller.apply_power_limit_w(
                int(requested_w)
            )
        except Exception as exc:
            self.requested_power_limit_w = None
            self.translated_gpu_policy.pop("power_limit_w", None)
            log(
                "Auto-UV power limit: unable to apply "
                f"{int(requested_w)}W for final verification; "
                "continuing without saved power limit: "
                f"{exc}"
            )
            return None
        self.translated_gpu_policy["power_limit_w"] = int(applied_power_limit_w)
        # Consume the request so a subsequent tier's apply starts clean and
        # never mistakes this tier's applied cap for a fresh scan-wide one.
        self.requested_power_limit_w = None
        log(
            "Auto-UV power limit: applied "
            f"{int(applied_power_limit_w)}W for final verification"
        )
        return int(applied_power_limit_w)

    def close(self) -> None:
        if self.clock_ceiling is not None:
            self.clock_ceiling.close()


def open_live_gpu_vf_curve_applier(
    *,
    gpu_index: int,
    runtime_options: dict,
    log: Callable[[str], None],
) -> LiveGpuVfCurveApplier:
    gpu = DaemonGpuClient(gpu_index=int(gpu_index))
    try:
        gpu.refresh_points()
    except Exception as exc:
        raise AutoUvError(
            "failed to create Linux NVAPI VF helper"
            f": {exc}. This driver/GPU combination may not expose editable "
            "voltage-based V/F points."
        ) from exc

    runtime_reset = reset_nvidia_runtime_defaults(
        gpu_index=int(gpu_index),
        log=log,
    )
    runtime_default_plan = list(runtime_reset["plan"])
    # The semantic daemon reset already applied and verified this zero plan.
    # Refresh the reader created above so the scan starts from that live state
    # without issuing the same privileged V/F write a second time.
    gpu.refresh_points()
    assert_zero_runtime_vf_offsets(gpu)

    baseline_power_limit_w = _positive_power_limit_w(runtime_reset.get("power_limit_w"))
    reset_power_limits = runtime_reset.get("power_limits")
    reset_power_limits = reset_power_limits if isinstance(reset_power_limits, dict) else {}
    min_power_limit_w = _positive_power_limit_w(
        reset_power_limits.get("power_limit_min_w")
    )
    max_power_limit_w = _positive_power_limit_w(
        reset_power_limits.get("power_limit_max_w")
    )
    translated_gpu_policy = {
        "gpu_name": runtime_reset.get("gpu_name"),
        "power_limit_w": baseline_power_limit_w,
    }
    requested_power_limit_w = _auto_uv_power_limit_w(runtime_options)
    if requested_power_limit_w is not None and (
        baseline_power_limit_w is None
        or int(requested_power_limit_w) >= int(baseline_power_limit_w)
    ):
        try:
            applied_power_limit_w = gpu.apply_power_limit_w(
                int(requested_power_limit_w)
            )
        except Exception as exc:
            translated_gpu_policy.pop("power_limit_w", None)
            log(
                "Auto-UV power limit: unable to apply "
                f"{int(requested_power_limit_w)}W; "
                "continuing without saved power limit: "
                f"{exc}"
            )
            requested_power_limit_w = None
            applied_power_limit_w = None
        if applied_power_limit_w is not None:
            translated_gpu_policy["power_limit_w"] = int(applied_power_limit_w)
            log(f"Auto-UV power limit: applied {int(applied_power_limit_w)}W")
    elif requested_power_limit_w is not None and baseline_power_limit_w is not None:
        log(
            "Auto-UV power limit: keeping "
            f"{int(baseline_power_limit_w)}W for discovery/sweep; "
            f"will apply {int(requested_power_limit_w)}W for final verification"
        )

    memory_offset_mhz, memory_offset_limit_mhz = auto_uv_memory_offset_mhz(
        runtime_options,
        policy_controller=gpu,
    )
    if memory_offset_mhz is not None:
        translated_gpu_policy["mem_clk_vf_offset_mhz"] = int(memory_offset_mhz)
        translated_gpu_policy["mem_clk_vf_offset_limit_mhz"] = int(
            memory_offset_limit_mhz
        )
        raw_memory_offset = runtime_options.get(
            "auto_uv_memory_offset_mhz",
            runtime_options.get("memory_offset_mhz"),
        )
        if raw_memory_offset not in (None, "") and int(raw_memory_offset) != int(
            memory_offset_mhz
        ):
            log(
                f"Auto-UV memory offset: requested {int(raw_memory_offset)} MHz "
                f"clamped to {int(memory_offset_mhz)} MHz "
                f"(limit {int(memory_offset_limit_mhz)} MHz)"
            )
        if int(memory_offset_mhz) != 0:
            applied_memory_offset = gpu.apply_clock_offsets(
                mem_clk_vf_offset_mhz=int(memory_offset_mhz)
            )
            readback_mhz = applied_memory_offset.get("mem_clk_vf_offset_readback_mhz")
            if readback_mhz is None:
                log(
                    f"Auto-UV memory offset: applied {int(memory_offset_mhz):+d} MHz "
                    "(driver does not support read-back)"
                )
            elif int(readback_mhz) == int(memory_offset_mhz):
                log(
                    f"Auto-UV memory offset: applied {int(memory_offset_mhz):+d} MHz, "
                    f"NVML read-back confirms {int(readback_mhz):+d} MHz"
                )
            else:
                log(
                    f"Auto-UV memory offset MISMATCH: requested "
                    f"{int(memory_offset_mhz):+d} MHz but NVML reads back "
                    f"{int(readback_mhz):+d} MHz -- the driver clamped or ignored it"
                )

    return LiveGpuVfCurveApplier(
        gpu_index=int(gpu_index),
        gpu=gpu,
        min_power_limit_w=min_power_limit_w,
        max_power_limit_w=max_power_limit_w,
        runtime_default_plan=runtime_default_plan,
        translated_gpu_policy=translated_gpu_policy,
        baseline_power_limit_w=baseline_power_limit_w,
        requested_power_limit_w=requested_power_limit_w,
    )


def _auto_uv_power_limit_w(runtime_options: dict) -> int | None:
    value = runtime_options.get("auto_uv_power_limit_w")
    if value in (None, ""):
        return None
    try:
        power_limit_w = int(round(float(cast(Any, value))))
    except (TypeError, ValueError) as exc:
        raise AutoUvError(f"invalid Auto-UV power limit: {value!r}") from exc
    return power_limit_w if power_limit_w > 0 else None


def _positive_power_limit_w(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        power_limit_w = int(round(float(cast(Any, value))))
    except (TypeError, ValueError):
        return None
    return power_limit_w if power_limit_w > 0 else None
