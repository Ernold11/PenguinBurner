"""Derive the base-load voltage band from the first Q2RTX telemetry.

The scan uses the loaded average voltage, snapped to a real base-curve bin, as the first flat-curve voltage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .base_load_telemetry import (
    LoadedTelemetryRules,
    decision_samples,
    derive_active_power_floor_w,
)
from ..shared.probe_data_fields import read_field


@dataclass(frozen=True, slots=True)
class LoadedVoltageBand:
    floor_mv: int | None
    average_mv: int | None
    ceiling_mv: int | None
    sample_count: int


@dataclass(frozen=True, slots=True)
class LoadedVoltageRules:
    floor_percentile: float = 0.10
    ceiling_percentile: float = 0.90


def derive_loaded_voltage_band(
    telemetry_samples: list[Any],
    *,
    power_limit_w: int | None,
    use_power_limit_floor: bool,
    telemetry_rules: LoadedTelemetryRules = LoadedTelemetryRules(),
    voltage_rules: LoadedVoltageRules = LoadedVoltageRules(),
) -> LoadedVoltageBand:
    samples = decision_samples(telemetry_samples, rules=telemetry_rules)
    active_power_floor_w = derive_active_power_floor_w(
        samples,
        power_limit_w=power_limit_w,
        use_power_limit_floor=use_power_limit_floor,
        rules=telemetry_rules,
    )
    if active_power_floor_w is None:
        return LoadedVoltageBand(None, None, None, 0)

    voltages = sorted(
        int(round(float(read_field(sample, "voltage_mv"))))
        for sample in samples
        if read_field(sample, "power_w") is not None
        and read_field(sample, "voltage_mv") is not None
        and float(read_field(sample, "power_w")) >= float(active_power_floor_w)
    )
    if not voltages:
        return LoadedVoltageBand(None, None, None, 0)

    return LoadedVoltageBand(
        floor_mv=pick_voltage_percentile(voltages, voltage_rules.floor_percentile),
        average_mv=int(round(sum(voltages) / float(len(voltages)))),
        ceiling_mv=pick_voltage_percentile(voltages, voltage_rules.ceiling_percentile),
        sample_count=len(voltages),
    )


def pick_voltage_percentile(values: list[int], percentile: float) -> int:
    if not values:
        raise ValueError("voltage percentile needs at least one value")
    sorted_values = sorted(int(value) for value in values)
    if len(sorted_values) == 1:
        return int(sorted_values[0])
    position = int(round((len(sorted_values) - 1) * float(percentile)))
    position = max(0, min(len(sorted_values) - 1, position))
    return int(sorted_values[position])
