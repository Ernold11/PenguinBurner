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
    sample_is_loaded,
)
from ..shared.probe_data_fields import read_field


@dataclass(frozen=True, slots=True)
class LoadedVoltageBand:
    average_mv: int | None


def derive_loaded_voltage_band(
    telemetry_samples: list[Any],
    *,
    power_limit_w: int | None,
    use_power_limit_floor: bool,
    telemetry_rules: LoadedTelemetryRules = LoadedTelemetryRules(),
) -> LoadedVoltageBand:
    samples = decision_samples(telemetry_samples, rules=telemetry_rules)
    active_power_floor_w = derive_active_power_floor_w(
        samples,
        power_limit_w=power_limit_w,
        use_power_limit_floor=use_power_limit_floor,
        rules=telemetry_rules,
    )

    voltages = sorted(
        int(round(float(read_field(sample, "voltage_mv"))))
        for sample in samples
        if read_field(sample, "voltage_mv") is not None
        and sample_is_loaded(
            sample,
            active_power_floor_w=active_power_floor_w,
            rules=telemetry_rules,
        )
    )
    if not voltages:
        return LoadedVoltageBand(None)

    return LoadedVoltageBand(
        average_mv=int(round(sum(voltages) / float(len(voltages)))),
    )
