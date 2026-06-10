from __future__ import annotations

import argparse
from pathlib import Path

from .state import read_overlay_state


def format_overlay_text(values: dict[str, str]) -> str:
    fps = _value_or_na(values.get("present_fps"), suffix=" FPS")
    clock = _value_or_na(values.get("clock_mhz"), suffix=" MHz")
    voltage = _value_or_na(values.get("voltage_mv"), suffix=" mV")
    tier = str(values.get("profile_tier") or "").strip() or "Balanced"
    return f"{fps} {clock} {voltage} {tier}"


def _value_or_na(value: object, *, suffix: str) -> str:
    text = str(value or "").strip()
    if not text or text.lower() == "n/a":
        return f"n/a{suffix}"
    if text.endswith(suffix):
        return text
    return f"{text}{suffix}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pb-overlay-text")
    parser.add_argument("--state", type=Path, default=None)
    args = parser.parse_args(argv)

    values = read_overlay_state(args.state)
    if not values:
        print("PB waiting")
    else:
        print(format_overlay_text(values))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
