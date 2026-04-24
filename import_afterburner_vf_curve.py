#!/usr/bin/env python3
"""Compatibility launcher for the Afterburner V/F curve importer."""

from afterburner.import_vf_curve import (
    AfterburnerProfileSelectionError,
    AfterburnerVfCurveSafetyError,
    main,
)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AfterburnerProfileSelectionError, AfterburnerVfCurveSafetyError) as exc:
        print(f"error: {exc}")
        raise SystemExit(2)
