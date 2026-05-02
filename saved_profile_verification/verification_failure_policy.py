"""Classify profile verification failures that should block future application.

User-requested stops are not a stability failure; other failures mark user-edited profiles unsafe.
"""

from __future__ import annotations


def profile_verification_failure_blocks_apply(reason: str) -> bool:
    text = str(reason or "").strip()
    if not text:
        return False
    if text.startswith("user-stop-requested"):
        return False
    return True
