from __future__ import annotations

from cli_output import CLI_OUTPUT_WRAP_COLUMNS
from penguin_burner_ui.components.log_view import _timestamp_log_text


def test_log_timestamp_is_added_to_each_line() -> None:
    rendered, at_line_start = _timestamp_log_text(
        "first\nsecond\n",
        timestamp="2026-04-27 12:34:56",
    )

    assert rendered == (
        "[2026-04-27 12:34:56] first\n"
        "[2026-04-27 12:34:56] second\n"
    )
    assert at_line_start


def test_log_timestamp_does_not_split_streamed_partial_line() -> None:
    first, at_line_start = _timestamp_log_text(
        "partial",
        timestamp="2026-04-27 12:34:56",
    )
    second, at_line_start = _timestamp_log_text(
        " done\nnext",
        timestamp="2026-04-27 12:34:57",
        at_line_start=at_line_start,
    )

    assert first == "[2026-04-27 12:34:56] partial"
    assert second == " done\n[2026-04-27 12:34:57] next"
    assert not at_line_start


def test_log_view_wraps_display_lines_to_160_columns() -> None:
    rendered, at_line_start = _timestamp_log_text(
        "long " + ("x" * 260) + "\n",
        timestamp="2026-04-27 12:34:56",
    )

    lines = rendered.splitlines()
    assert len(lines) >= 2
    assert all(len(line) <= CLI_OUTPUT_WRAP_COLUMNS for line in lines)
    assert lines[0].startswith("[2026-04-27 12:34:56] long ")
    assert at_line_start
