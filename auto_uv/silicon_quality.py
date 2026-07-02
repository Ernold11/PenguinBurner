"""Grade a scan's final V/F point against the GPU's reference V/F line.

The reference line is the efficiency->performance segment from the borrowed
per-GPU targets. We measure how far the achieved (voltage, clock) point beats
the clock the reference line predicts at that voltage: a chip that holds more
clock (or the same clock at less voltage) lands above the line. This is
mode-agnostic - efficiency lowers voltage at a held clock, performance raises
clock - because both simply move the achieved point off the reference line.
"""

from __future__ import annotations

from dataclasses import dataclass

from auto_uv.scan_mode.uv_limits import uv_limit_profile_target_for_gpu


# Delta (achieved clock minus reference-line clock at the achieved voltage), in
# MHz, that separates the quality buckets. Positive means better-than-reference
# silicon. Thresholds are a few clock bins wide so a single snapped step does not
# flip the grade.
_EXCELLENT_MIN_MHZ = 45.0
_ABOVE_AVERAGE_MIN_MHZ = 0.0
_AVERAGE_MIN_MHZ = -75.0

_GRADE_LABELS = {
    "excellent": "Excellent",
    "above-average": "Above average",
    "average": "Average",
    "below-average": "Below average",
}


@dataclass(frozen=True, slots=True)
class SiliconQuality:
    grade: str
    label: str
    delta_mhz: int
    voltage_mv: int
    clock_mhz: int
    reference_clock_mhz: int
    gpu_family: str

    def summary_text(self) -> str:
        sign = "+" if self.delta_mhz >= 0 else ""
        return (
            f"{self.label} ({sign}{self.delta_mhz} MHz vs reference "
            f"@ {self.voltage_mv} mV)"
        )


def assess_silicon_quality(
    *,
    gpu_name: object | None,
    voltage_mv: int,
    clock_mhz: int,
) -> SiliconQuality | None:
    """Grade the final point, or None when the GPU has no reference targets."""
    efficiency = uv_limit_profile_target_for_gpu(gpu_name, "efficiency")
    performance = uv_limit_profile_target_for_gpu(gpu_name, "performance")
    if efficiency is None or performance is None:
        return None
    if int(voltage_mv) <= 0 or int(clock_mhz) <= 0:
        return None

    ref_low_v = int(efficiency.voltage_mv)
    ref_low_c = int(efficiency.clock_mhz)
    ref_high_v = int(performance.voltage_mv)
    ref_high_c = int(performance.clock_mhz)
    if ref_high_v == ref_low_v:
        return None

    slope = float(ref_high_c - ref_low_c) / float(ref_high_v - ref_low_v)
    expected_clock = float(ref_low_c) + slope * (float(voltage_mv) - float(ref_low_v))
    delta = float(clock_mhz) - expected_clock
    grade = _grade_for_delta(delta)
    return SiliconQuality(
        grade=grade,
        label=_GRADE_LABELS[grade],
        delta_mhz=int(round(delta)),
        voltage_mv=int(voltage_mv),
        clock_mhz=int(clock_mhz),
        reference_clock_mhz=int(round(expected_clock)),
        gpu_family=str(efficiency.gpu_family),
    )


def _grade_for_delta(delta_mhz: float) -> str:
    if delta_mhz >= _EXCELLENT_MIN_MHZ:
        return "excellent"
    if delta_mhz >= _ABOVE_AVERAGE_MIN_MHZ:
        return "above-average"
    if delta_mhz >= _AVERAGE_MIN_MHZ:
        return "average"
    return "below-average"
