from __future__ import annotations

from .samples import _int_value
from .samples import _positive_us

FRAMEGEN_ACTIVE_KEYS = ("framegen_active", "frame_generation_active", "fg_active")
FRAMEGEN_MARKER_KEYS = ("framegen_frame", "generated_frame", "fg_frame")
FRAMEGEN_COUNT_KEYS = (
    "framegen_frame_count",
    "generated_frame_count",
    "fg_frame_count",
)
FRAMEGEN_MEASUREMENTS = {"framegen-marker", "framegen-frame", "generated-frame"}
# Output (present) cadence must exceed the base render cadence by at least this
# ratio before the overlay reports frame generation. Out-of-band present markers
# and the sim->displayed-present span are emitted by Reflex low-latency mode with
# frame generation OFF, so marker presence alone is not evidence of generated
# frames -- only a multiplied display rate is.
FRAMEGEN_CADENCE_RATIO = 1.5
FRAMEGEN_MARKER_NAMES = {
    "oob-present-start",
    "oob-present-end",
    "out-of-band-present-start",
    "out-of-band-present-end",
    "out_of_band_present_start",
    "out_of_band_present_end",
}


def _truthy_value(value: object) -> bool:
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "on", "active"}


def _explicit_framegen_active(samples: list[dict]) -> bool:
    for sample in samples:
        if any(_truthy_value(sample.get(key)) for key in FRAMEGEN_ACTIVE_KEYS):
            return True
        if any(_truthy_value(sample.get(key)) for key in FRAMEGEN_MARKER_KEYS):
            return True
        if any(_int_value(sample.get(key)) > 0 for key in FRAMEGEN_COUNT_KEYS):
            return True
        marker_name = str(sample.get("marker_name") or "").strip().lower()
        if marker_name in FRAMEGEN_MARKER_NAMES:
            return True
        if _positive_us(sample, "sim_to_oob_present_us") or _positive_us(
            sample, "input_to_oob_present_us"
        ):
            return True
        measurement = str(sample.get("measurement") or "").strip().lower()
        if measurement in FRAMEGEN_MEASUREMENTS:
            return True
    return False


def _framegen_active_for_overlay(
    samples: list[dict],
    *,
    base_fps: float | None,
    raw_output_fps: float | None,
    cadence_is_independent: bool,
) -> bool:
    """Whether to report frame generation on the overlay.

    Frame generation is confirmed only when the displayed cadence clearly
    exceeds the base render cadence. Reflex out-of-band present markers and the
    sim->displayed-present span are deliberately not evidence by themselves.
    """
    base = _safe_float(base_fps)
    output = _safe_float(raw_output_fps)
    if (
        cadence_is_independent
        and base
        and output
        and base > 0
        and output >= base * FRAMEGEN_CADENCE_RATIO
    ):
        return True

    for sample in samples:
        if any(_truthy_value(sample.get(key)) for key in FRAMEGEN_ACTIVE_KEYS):
            return True
        if any(_truthy_value(sample.get(key)) for key in FRAMEGEN_MARKER_KEYS):
            return True
        if any(_int_value(sample.get(key)) > 0 for key in FRAMEGEN_COUNT_KEYS):
            return True
        measurement = str(sample.get("measurement") or "").strip().lower()
        if measurement in FRAMEGEN_MEASUREMENTS:
            return True
    return False


def _has_marker_stream(samples: list[dict]) -> bool:
    """Whether the window carries a Reflex/nvapi marker stream."""
    for sample in samples:
        if _int_value(sample.get("marker_bits")):
            return True
        if str(sample.get("measurement") or "") == "marker-proxy":
            return True
        if _int_value(sample.get("base_frame_frametime_us")) > 0:
            return True
        if _positive_us(sample, "sim_to_present_us") or _positive_us(
            sample, "sim_to_oob_present_us"
        ):
            return True
    return False


def _safe_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
