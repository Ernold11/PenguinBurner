"""Parse Auto-UV profile selectors from raw runtime argv.

These helpers run before argparse so CLI startup can fail early for missing profile ids.
"""

from __future__ import annotations


def runtime_profile_selector_from_argv(argv) -> str:
    index = 0
    while index < len(argv):
        arg = str(argv[index])
        if arg == "--auto-uv-profile" and index + 1 < len(argv):
            return str(argv[index + 1]).strip()
        if arg.startswith("--auto-uv-profile="):
            return arg.split("=", 1)[1].strip()
        index += 1
    return ""


def runtime_profile_selector_allows_unverified_from_argv(argv) -> bool:
    return any(str(arg) == "--stability-test" for arg in argv)
