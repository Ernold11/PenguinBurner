from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


class AutoUvError(RuntimeError):
    pass


@dataclass(slots=True)
class AutoUvProbeSummary:
    candidate_voltage_mv: int
    lock_clock_mhz: int
    live_voltage_before_mv: int | None
    live_voltage_after_mv: int | None
    avg_voltage_mv: float | None
    frames_per_run: int | None
    avg_seconds_per_run: float | None
    avg_fps: float | None
    min_fps: float | None
    max_fps: float | None
    avg_power_w: float | None
    max_power_w: float | None
    avg_temperature_c: float | None
    max_temperature_c: float | None
    avg_fan_speed_pct: float | None
    max_fan_speed_pct: float | None
    avg_core_clock_mhz: float | None
    efficiency_fps_per_w: float | None
    efficiency_mhz_per_w: float | None
    watts_per_mhz: float | None
    used_companion_load: bool
    result_reason: str
    log_path: Path


@dataclass(slots=True)
class AutoUvVoltageScanResult:
    success: bool
    final_voltage_mv: int
    lock_clock_mhz: int
    stop_reason: str
    failed_candidate_voltage_mv: int | None
    probes: list[AutoUvProbeSummary]
    baseline_core_clock_mhz: float | None = None
    baseline_power_w: float | None = None
    baseline_temperature_c: float | None = None
    baseline_fan_speed_pct: float | None = None
    baseline_efficiency_mhz_per_w: float | None = None
    final_core_clock_mhz: float | None = None
    final_power_w: float | None = None
    final_temperature_c: float | None = None
    final_fan_speed_pct: float | None = None
    final_efficiency_mhz_per_w: float | None = None
    core_clock_drop_mhz: float | None = None
    core_clock_drop_pct: float | None = None
    power_saved_w: float | None = None
    power_saved_pct: float | None = None


@dataclass(slots=True)
class AutoUvCurveCandidate:
    label: str
    candidate_voltage_mv: int
    target_clock_mhz: int
    plan: list[dict]


@dataclass(slots=True)
class VoltagePoint:
    index: int
    voltage_mv: int
    base_mhz: int
    target_mhz: int
    preserve_vanilla: bool = False
    source: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_plan_item(cls, item: dict) -> VoltagePoint:
        return cls(
            index=int(item["index"]),
            voltage_mv=int(item["voltage_mv"]),
            base_mhz=int(item["base_mhz"]),
            target_mhz=int(item["target_mhz"]),
            preserve_vanilla=bool(item.get("preserve_vanilla")),
            source=dict(item),
        )

    @property
    def new_offset_mhz(self) -> int:
        return int(self.target_mhz) - int(self.base_mhz)

    def with_target_mhz(self, target_mhz: int) -> VoltagePoint:
        return VoltagePoint(
            index=int(self.index),
            voltage_mv=int(self.voltage_mv),
            base_mhz=int(self.base_mhz),
            target_mhz=int(target_mhz),
            preserve_vanilla=bool(self.preserve_vanilla),
            source=dict(self.source),
        )

    def to_plan_item(self) -> dict:
        item = dict(self.source)
        item["index"] = int(self.index)
        item["voltage_mv"] = int(self.voltage_mv)
        item["base_mhz"] = int(self.base_mhz)
        item["target_mhz"] = int(self.target_mhz)
        item["new_offset_mhz"] = int(self.new_offset_mhz)
        if self.preserve_vanilla or "preserve_vanilla" in item:
            item["preserve_vanilla"] = bool(self.preserve_vanilla)
        return item

    def to_artifact_point(self) -> dict:
        return {
            "index": int(self.index),
            "voltage_mv": int(self.voltage_mv),
            "base_mhz": int(self.base_mhz),
            "target_mhz": int(self.target_mhz),
            "new_offset_mhz": int(self.new_offset_mhz),
        }


@dataclass(frozen=True, slots=True)
class VoltageCurve:
    points: tuple[VoltagePoint, ...]

    @classmethod
    def from_plan(cls, plan: list[dict]) -> VoltageCurve:
        return cls(tuple(VoltagePoint.from_plan_item(item) for item in plan))

    @property
    def voltage_bins(self) -> list[int]:
        return sorted({int(point.voltage_mv) for point in self.points})

    @property
    def editable_points(self) -> list[VoltagePoint]:
        return [
            point
            for point in sorted(self.points, key=lambda item: int(item.voltage_mv))
            if not point.preserve_vanilla
        ]

    @property
    def editable_voltage_bins(self) -> list[int]:
        return [int(point.voltage_mv) for point in self.editable_points]

    def nearest_voltage_bin(self, voltage_mv: int) -> int:
        available = self.voltage_bins
        if not available:
            raise ValueError("voltage curve did not contain any voltage bins")
        return int(min(available, key=lambda value: abs(int(value) - int(voltage_mv))))

    def lower_voltage_bins(
        self,
        start_voltage_mv: int,
        *,
        preserve_vanilla_below_mv: int | None,
        min_search_voltage_mv: int | None = None,
    ) -> list[int]:
        bins: list[int] = []
        for voltage_mv in self.editable_voltage_bins:
            if voltage_mv >= int(start_voltage_mv):
                continue
            if preserve_vanilla_below_mv is not None and int(voltage_mv) <= int(
                preserve_vanilla_below_mv
            ):
                continue
            if min_search_voltage_mv is not None and int(voltage_mv) < int(
                min_search_voltage_mv
            ):
                continue
            bins.append(int(voltage_mv))
        bins.sort(reverse=True)
        return bins

    def next_higher_voltage_bin(self, voltage_mv: int) -> int | None:
        higher = [
            value
            for value in self.editable_voltage_bins
            if int(value) > int(voltage_mv)
        ]
        return int(min(higher)) if higher else None

    def higher_voltage_bins(self, voltage_mv: int) -> list[int]:
        return [
            int(value)
            for value in self.editable_voltage_bins
            if int(value) > int(voltage_mv)
        ]

    def artifact_points(self) -> list[dict]:
        return [point.to_artifact_point() for point in self.points]
