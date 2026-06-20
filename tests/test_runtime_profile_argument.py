"""Tests for the pre-argparse Auto-UV profile selector helpers.

These run on raw argv before argparse so CLI startup can resolve (or reject) a
profile selector early.
"""

from __future__ import annotations

from cli.runtime_profile_argument import (
    runtime_profile_selector_allows_unverified_from_argv,
    runtime_profile_selector_from_argv,
)


def test_selector_reads_space_separated_value() -> None:
    argv = ["--auto-uv-voltage-scan", "--auto-uv-profile", "  latest  "]
    assert runtime_profile_selector_from_argv(argv) == "latest"


def test_selector_reads_equals_form() -> None:
    argv = ["--auto-uv-profile=active", "--daemonize"]
    assert runtime_profile_selector_from_argv(argv) == "active"


def test_selector_trailing_flag_without_value_is_empty() -> None:
    # --auto-uv-profile as the last token has no value to consume.
    assert runtime_profile_selector_from_argv(["--auto-uv-profile"]) == ""


def test_selector_absent_returns_empty() -> None:
    assert runtime_profile_selector_from_argv(["--daemonize"]) == ""


def test_allows_unverified_only_with_stability_test() -> None:
    assert (
        runtime_profile_selector_allows_unverified_from_argv(
            ["--stability-test", "--auto-uv-profile=latest"]
        )
        is True
    )
    assert (
        runtime_profile_selector_allows_unverified_from_argv(["--auto-uv-profile=latest"])
        is False
    )
