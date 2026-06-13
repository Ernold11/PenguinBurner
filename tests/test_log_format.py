"""Coverage for the shared concise log-line helper (common/log_format.py)."""

from __future__ import annotations

from common.log_format import format_log_line, format_sections


def test_format_sections_joins_with_pipe_and_drops_empty() -> None:
    assert format_sections(["a", "b", "c"]) == "a | b | c"
    assert format_sections(["a", None, "", "  ", "d"]) == "a | d"
    assert format_sections([]) == ""


def test_format_log_line_tags_and_pads() -> None:
    assert (
        format_log_line("status", "58C", "257W", "fan 35% manual", "Balanced")
        == "status | 58C | 257W | fan 35% manual | Balanced"
    )
    # Tag is left-padded to the width of the widest tag ("status").
    assert format_log_line("tier", "Balanced → Performance") == "tier   | Balanced → Performance"


def test_format_log_line_drops_empty_sections() -> None:
    assert format_log_line("warn", "overlay publish unavailable", None, "") == (
        "warn   | overlay publish unavailable"
    )


def test_format_log_line_without_tag_is_body_only() -> None:
    assert format_log_line("", "x", "y") == "x | y"


def test_format_log_line_tag_only_when_no_sections() -> None:
    assert format_log_line("status") == "status"
    assert format_log_line("status", None, "") == "status"


def test_format_log_line_coerces_non_strings() -> None:
    assert format_log_line("status", 58, 257) == "status | 58 | 257"
