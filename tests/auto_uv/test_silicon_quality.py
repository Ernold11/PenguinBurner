from __future__ import annotations

from auto_uv.main_loop import emit_silicon_quality
from auto_uv.silicon_quality import assess_silicon_quality


# RTX 5080 reference V/F line: efficiency (850 mV, 2800 MHz) -> performance
# (925 mV, 2980 MHz), slope = 180/75 = 2.4 MHz/mV.


def test_low_voltage_hold_of_the_clock_grades_excellent() -> None:
    # Holds the efficiency clock 30 mV below the efficiency point: the reference
    # line predicts 2800 - 2.4*30 = 2728 MHz there, so +72 MHz over reference.
    quality = assess_silicon_quality(
        gpu_name="NVIDIA GeForce RTX 5080", voltage_mv=820, clock_mhz=2800
    )
    assert quality is not None
    assert quality.grade == "excellent"
    assert quality.label == "Excellent"
    assert quality.delta_mhz == 72
    assert quality.reference_clock_mhz == 2728
    assert quality.gpu_family == "RTX 5080"
    assert "+72 MHz" in quality.summary_text()


def test_point_on_the_reference_line_grades_above_average() -> None:
    quality = assess_silicon_quality(
        gpu_name="NVIDIA GeForce RTX 5080", voltage_mv=850, clock_mhz=2800
    )
    assert quality is not None
    assert quality.delta_mhz == 0
    assert quality.grade == "above-average"


def test_weak_chip_below_the_line_grades_below_average() -> None:
    # 2700 MHz at 900 mV: reference predicts 2800 + 2.4*50 = 2920, so -220 MHz.
    quality = assess_silicon_quality(
        gpu_name="NVIDIA GeForce RTX 5080", voltage_mv=900, clock_mhz=2700
    )
    assert quality is not None
    assert quality.delta_mhz == -220
    assert quality.grade == "below-average"
    assert quality.summary_text().startswith("Below average (-220 MHz")


def test_average_band_between_zero_and_floor() -> None:
    # 40 MHz under the line at the efficiency voltage -> within the average band.
    quality = assess_silicon_quality(
        gpu_name="NVIDIA GeForce RTX 5080", voltage_mv=850, clock_mhz=2760
    )
    assert quality is not None
    assert quality.delta_mhz == -40
    assert quality.grade == "average"


def test_unlisted_gpu_has_no_grade() -> None:
    assert (
        assess_silicon_quality(
            gpu_name="NVIDIA GeForce GTX 1080", voltage_mv=850, clock_mhz=1800
        )
        is None
    )


def test_emit_silicon_quality_publishes_event_and_logs() -> None:
    events: list[tuple[str, dict]] = []
    logs: list[str] = []

    emit_silicon_quality(
        gpu_name="NVIDIA GeForce RTX 5080",
        voltage_mv=820,
        clock_mhz=2800,
        log=logs.append,
        event_callback=lambda event, payload: events.append((event, payload)),
    )

    assert len(events) == 1
    event, payload = events[0]
    assert event == "silicon_quality"
    assert payload["grade"] == "excellent"
    assert payload["delta_mhz"] == 72
    assert "Excellent" in payload["summary"]
    assert any("Silicon quality" in line for line in logs)


def test_emit_silicon_quality_is_silent_for_unlisted_gpu() -> None:
    events: list[tuple[str, dict]] = []

    emit_silicon_quality(
        gpu_name="NVIDIA GeForce GTX 1080",
        voltage_mv=850,
        clock_mhz=1800,
        log=lambda _line: None,
        event_callback=lambda event, payload: events.append((event, payload)),
    )

    assert events == []


def test_non_positive_inputs_are_rejected() -> None:
    assert (
        assess_silicon_quality(
            gpu_name="NVIDIA GeForce RTX 5080", voltage_mv=0, clock_mhz=2800
        )
        is None
    )
    assert (
        assess_silicon_quality(
            gpu_name="NVIDIA GeForce RTX 5080", voltage_mv=850, clock_mhz=0
        )
        is None
    )
