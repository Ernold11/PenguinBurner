#!/usr/bin/env python3
"""Compatibility launcher for the Q2RTX stability-test CLI."""

from stability.q2rtx import main


if __name__ == "__main__":
    raise SystemExit(main())
