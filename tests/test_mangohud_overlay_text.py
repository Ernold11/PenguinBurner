from __future__ import annotations

from penguin_burner_overlay.mangohud_text import format_overlay_text


def test_mangohud_overlay_text_formats_compact_line() -> None:
    assert (
        format_overlay_text(
            {
                "present_fps": "58",
                "clock_mhz": "2542",
                "voltage_mv": "950",
                "profile_tier": "Balanced",
            }
        )
        == "58 FPS 2542 MHz 950 mV Balanced"
    )


def test_mangohud_overlay_text_handles_missing_values() -> None:
    assert format_overlay_text({}) == "n/a FPS n/a MHz n/a mV Balanced"
