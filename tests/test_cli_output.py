from __future__ import annotations

import io

from common.cli_output import (
    CLI_OUTPUT_WRAP_COLUMNS,
    WrappedOutputStream,
    wrap_cli_output_text,
)


def test_cli_output_wraps_human_lines_to_160_columns() -> None:
    text = "prefix " + "x" * 240 + "\n"

    wrapped = wrap_cli_output_text(text)

    lines = wrapped.splitlines()
    assert len(lines) >= 2
    assert all(len(line) <= CLI_OUTPUT_WRAP_COLUMNS for line in lines)


def test_cli_output_preserves_json_documents() -> None:
    text = '{"event": "probe_result", "long": "' + ("x" * 240) + '"}\n'

    assert wrap_cli_output_text(text) == text


def test_wrapped_output_stream_reports_original_write_length() -> None:
    sink = io.StringIO()
    stream = WrappedOutputStream(sink)
    text = "x" * 240

    assert stream.write(text) == len(text)
    assert all(
        len(line) <= CLI_OUTPUT_WRAP_COLUMNS
        for line in sink.getvalue().splitlines()
    )
