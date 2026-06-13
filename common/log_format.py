"""Concise single-line log formatting shared by the runtime daemon and the
Auto-UV scan.

A record is a short left-aligned ``tag`` (so lines are self-describing and easy
to ``grep``/filter by kind) followed by sections joined with `` | ``. Empty or
None sections are dropped so optional fields never leave dangling separators.

    >>> format_log_line("status", "58C", "257W", "fan 35% manual", "Balanced")
    'status | 58C | 257W | fan 35% manual | Balanced'
    >>> format_log_line("warn", "overlay publish unavailable", None, "")
    'warn   | overlay publish unavailable'
"""

from __future__ import annotations

from typing import Iterable

# Widest tag we emit ("status") so the section columns line up across records.
_TAG_WIDTH = 6


def format_sections(sections: Iterable[object]) -> str:
    """Join non-empty sections with `` | ``."""
    return " | ".join(
        str(section)
        for section in sections
        if section is not None and str(section).strip()
    )


def format_log_line(tag: str, *sections: object) -> str:
    """Render one log record as ``<tag> | <section> | <section> …``.

    With no tag, just the joined sections are returned.
    """
    body = format_sections(sections)
    tag = str(tag).strip()
    if not tag:
        return body
    if not body:
        return tag
    return f"{tag:<{_TAG_WIDTH}} | {body}"
