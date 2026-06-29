"""Derive base-load telemetry values from the first Q2RTX discovery run.

It ignores ramp-up/idle samples so curve flattening preserves sustained load clocks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..shared.probe_data_fields import numeric_values, percent, read_field


@dataclass(frozen=True, slots=True)
class LoadedTelemetryRules:
    loaded_sample_warmup_s: float = 5.0
    saturated_tail_power_pct: float = 90.0
    saturated_tail_core_clock_pct: float = 98.0
    saturated_tail_min_samples: int = 2
    power_saturation_headroom_pct: float = 2.0
    loaded_sample_power_floor_pct: float = 75.0
    loaded_sample_gpu_util_pct: float = 60.0
    active_core_clock_percentile: float = 0.75


def decision_samples(samples: list[Any], *, rules: LoadedTelemetryRules) -> list[Any]:
    # Ramp-up samples are not steady load; keep raw samples only when filtering would erase all evidence.
    warmup_s = max(0.0, float(rules.loaded_sample_warmup_s))
    if warmup_s <= 0.0:
        return list(samples)
    filtered = [
        sample
        for sample in samples
        if read_field(sample, "elapsed_s") is not None
        and float(read_field(sample, "elapsed_s")) >= warmup_s
    ]
    return filtered or list(samples)


def saturated_tail_samples(
    telemetry_samples: list[Any],
    *,
    rules: LoadedTelemetryRules = LoadedTelemetryRules(),
) -> list[Any]:
    samples = decision_samples(telemetry_samples, rules=rules)
    power_values = sample_values(samples, "power_w")
    clock_values = sample_values(samples, "core_clock_mhz")
    if not power_values or not clock_values:
        return samples

    power_floor_w = max(power_values) * percent(rules.saturated_tail_power_pct)
    clock_floor_mhz = max(clock_values) * percent(rules.saturated_tail_core_clock_pct)

    def is_saturated(sample: Any) -> bool:
        power_w = read_field(sample, "power_w")
        clock_mhz = read_field(sample, "core_clock_mhz")
        return (
            power_w is not None
            and clock_mhz is not None
            and float(power_w) >= float(power_floor_w)
            and float(clock_mhz) >= float(clock_floor_mhz)
        )

    # The final continuous saturated section is the best estimate of settled Q2RTX load.
    tail_reversed = []
    for sample in reversed(samples):
        if is_saturated(sample):
            tail_reversed.append(sample)
            continue
        if tail_reversed:
            break
    tail = list(reversed(tail_reversed))
    min_samples = max(1, int(rules.saturated_tail_min_samples))
    if len(tail) >= min_samples:
        return tail

    saturated = [sample for sample in samples if is_saturated(sample)]
    return saturated if len(saturated) >= min_samples else samples


def derive_power_saturated_clock_mhz(
    telemetry_samples: list[Any],
    *,
    power_limit_w: int | None,
    rules: LoadedTelemetryRules = LoadedTelemetryRules(),
) -> tuple[float | None, int, float | None]:
    if power_limit_w is None or int(power_limit_w) <= 0:
        return None, 0, None
    floor_w = float(power_limit_w) * (
        1.0 - percent(rules.power_saturation_headroom_pct)
    )
    clocks = [
        float(read_field(sample, "core_clock_mhz"))
        for sample in decision_samples(telemetry_samples, rules=rules)
        if read_field(sample, "power_w") is not None
        and read_field(sample, "core_clock_mhz") is not None
        and float(read_field(sample, "power_w")) >= float(floor_w)
    ]
    if not clocks:
        return None, 0, float(floor_w)
    return sum(clocks) / len(clocks), len(clocks), float(floor_w)


def derive_active_core_clock_mhz(
    telemetry_samples: list[Any],
    *,
    power_limit_w: int | None,
    use_power_limit_floor: bool,
    rules: LoadedTelemetryRules = LoadedTelemetryRules(),
) -> tuple[float | None, float | None, int, float | None]:
    samples = decision_samples(telemetry_samples, rules=rules)
    active_power_floor_w = derive_active_power_floor_w(
        samples,
        power_limit_w=power_limit_w,
        use_power_limit_floor=use_power_limit_floor,
        rules=rules,
    )
    # Only loaded samples choose the flatten clock; idle/menu samples must not lower it.
    clocks = sorted(
        float(read_field(sample, "core_clock_mhz"))
        for sample in samples
        if read_field(sample, "core_clock_mhz") is not None
        and sample_is_loaded(
            sample,
            active_power_floor_w=active_power_floor_w,
            rules=rules,
        )
    )
    if not clocks:
        return (
            None,
            None,
            0,
            float(active_power_floor_w)
            if active_power_floor_w is not None
            else None,
        )
    avg_clock_mhz = sum(clocks) / len(clocks)
    preferred_clock_mhz = percentile(
        clocks,
        float(rules.active_core_clock_percentile),
    )
    return (
        float(avg_clock_mhz),
        float(preferred_clock_mhz),
        len(clocks),
        float(active_power_floor_w),
    )


def derive_active_power_floor_w(
    telemetry_samples: list[Any],
    *,
    power_limit_w: int | None,
    use_power_limit_floor: bool,
    rules: LoadedTelemetryRules = LoadedTelemetryRules(),
) -> float | None:
    power_values = sample_values(
        decision_samples(telemetry_samples, rules=rules),
        "power_w",
    )
    if not power_values:
        return None
    if use_power_limit_floor and power_limit_w is not None and int(power_limit_w) > 0:
        return float(power_limit_w) * percent(rules.loaded_sample_power_floor_pct)
    return max(power_values) * percent(rules.loaded_sample_power_floor_pct)


def sample_is_loaded(
    sample: Any,
    *,
    active_power_floor_w: float | None,
    rules: LoadedTelemetryRules = LoadedTelemetryRules(),
) -> bool:
    if sample is None:
        return False
    gpu_util_pct = read_field(sample, "gpu_util_pct")
    if (
        gpu_util_pct is not None
        and float(gpu_util_pct) >= float(rules.loaded_sample_gpu_util_pct)
    ):
        return True
    power_w = read_field(sample, "power_w")
    return (
        power_w is not None
        and active_power_floor_w is not None
        and float(power_w) >= float(active_power_floor_w)
    )


def sample_values(samples: list[Any], field_name: str) -> list[float]:
    return numeric_values(samples, field_name)


def percentile(values: list[float], percentile_value: float) -> float:
    if not values:
        raise ValueError("percentile needs at least one value")
    sorted_values = sorted(float(value) for value in values)
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = int(round((len(sorted_values) - 1) * float(percentile_value)))
    position = max(0, min(len(sorted_values) - 1, position))
    return float(sorted_values[position])
