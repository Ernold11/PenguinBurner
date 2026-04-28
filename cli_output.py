from __future__ import annotations

import json
import sys


CLI_OUTPUT_WRAP_COLUMNS = 160


class WrappedOutputStream:
    _penguin_burner_cli_output_wrapped = True

    def __init__(self, original, *, width: int = CLI_OUTPUT_WRAP_COLUMNS):
        self._original = original
        self._width = int(width)

    def write(self, text):
        raw = str(text)
        self._original.write(
            wrap_cli_output_text(
                raw,
                width=self._width,
                preserve_json_documents=True,
                preserve_json_lines=True,
            )
        )
        return len(raw)

    def flush(self):
        self._original.flush()

    def isatty(self):
        return self._original.isatty()

    @property
    def encoding(self):
        return getattr(self._original, "encoding", "utf-8")

    @property
    def errors(self):
        return getattr(self._original, "errors", "replace")

    def __getattr__(self, name):
        return getattr(self._original, name)


def enable_cli_output_wrapping(*, width: int = CLI_OUTPUT_WRAP_COLUMNS) -> None:
    if not getattr(sys.stdout, "_penguin_burner_cli_output_wrapped", False):
        sys.stdout = WrappedOutputStream(sys.stdout, width=int(width))
    if not getattr(sys.stderr, "_penguin_burner_cli_output_wrapped", False):
        sys.stderr = WrappedOutputStream(sys.stderr, width=int(width))


def wrap_cli_output_text(
    text: str,
    *,
    width: int = CLI_OUTPUT_WRAP_COLUMNS,
    preserve_json_documents: bool = True,
    preserve_json_lines: bool = True,
) -> str:
    raw = str(text)
    if int(width) <= 0 or not raw:
        return raw
    if preserve_json_documents and _is_json_document(raw):
        return raw

    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    parts: list[str] = []
    for segment in normalized.splitlines(keepends=True):
        has_newline = segment.endswith("\n")
        line = segment[:-1] if has_newline else segment
        if (
            preserve_json_lines
            and len(line) > int(width)
            and _is_json_document(line)
        ):
            parts.append(line)
            if has_newline:
                parts.append("\n")
            continue
        wrapped = _wrap_output_line(line, width=int(width))
        if not wrapped:
            if has_newline:
                parts.append("\n")
            continue
        for index, item in enumerate(wrapped):
            parts.append(item)
            if has_newline or index < len(wrapped) - 1:
                parts.append("\n")
    return "".join(parts)


def _wrap_output_line(line: str, *, width: int) -> list[str]:
    if len(line) <= int(width):
        return [line]

    chunks: list[str] = []
    remaining = line
    width = int(width)
    soft_floor = max(1, width // 2)
    while len(remaining) > width:
        break_at = remaining.rfind(" ", 0, width + 1)
        if break_at < soft_floor:
            break_at = width
        chunk = remaining[:break_at].rstrip()
        if not chunk:
            chunk = remaining[:width]
            break_at = width
        chunks.append(chunk)
        remaining = remaining[break_at:].lstrip()
    chunks.append(remaining)
    return chunks


def _is_json_document(text: str) -> bool:
    stripped = str(text or "").strip()
    if not stripped or stripped[0] not in "[{":
        return False
    try:
        json.loads(stripped)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return True
