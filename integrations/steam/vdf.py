"""Minimal text-VDF parsing for Steam config files.

Steam's text VDF (loginusers.vdf, config.vdf, appmanifest_*.acf) is quoted
key/value pairs with brace-nested blocks. Steam has historically written the
same keys with varying case (``apps``/``Apps``, ``Software``/``software``),
so lookups must be case-insensitive.
"""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(
    r'"((?:\\.|[^"\\])*)"'  # quoted string, honoring \" and \\ escapes
    r"|([{}])"
    r"|(//[^\n]*)"
)


def _unescape(value: str) -> str:
    return re.sub(r'\\(["\\])', r"\1", value)


_QUOTED_RE = re.compile(r'"((?:\\.|[^"\\])*)"')


def quoted_tokens(text: str) -> list[str]:
    """The quoted strings on one VDF line, unescaped.

    Escapes are not optional here. Steam writes a launch line containing
    ``LD_PRELOAD=""`` as ``"LaunchOptions" "LD_PRELOAD=\\"\\" PROTON_..."``,
    and a pattern that stops at any quote reads that as ``LD_PRELOAD=\\`` and
    drops the rest of the user's command -- which is then what gets written
    back the next time the wrapper is toggled.
    """
    return [_unescape(match.group(1)) for match in _QUOTED_RE.finditer(text)]


def parse_vdf(text: str) -> dict[str, object]:
    """Parse text VDF into nested dicts (values are str or dict)."""
    root: dict[str, object] = {}
    stack: list[dict[str, object]] = [root]
    pending_key: str | None = None
    for match in _TOKEN_RE.finditer(text):
        quoted, brace, comment = match.groups()
        if comment is not None:
            continue
        if brace == "{":
            child: dict[str, object] = {}
            if pending_key is not None:
                stack[-1][pending_key] = child
                pending_key = None
            stack.append(child)
            continue
        if brace == "}":
            if len(stack) > 1:
                stack.pop()
            pending_key = None
            continue
        value = _unescape(quoted)
        if pending_key is None:
            pending_key = value
        else:
            stack[-1][pending_key] = value
            pending_key = None
    return root


def vdf_lookup(node: object, *keys: str) -> object | None:
    """Case-insensitive nested lookup; returns None on any miss."""
    current = node
    for key in keys:
        if not isinstance(current, dict):
            return None
        lowered = key.lower()
        found: object | None = None
        for candidate, value in current.items():
            if str(candidate).lower() == lowered:
                found = value
                break
        if found is None:
            return None
        current = found
    return current
