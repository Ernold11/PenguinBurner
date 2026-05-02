from __future__ import annotations

import ui.commands as commands


def test_ui_scan_command_can_enable_auto_uv3_for_testing(monkeypatch) -> None:
    monkeypatch.setattr(commands.os, "geteuid", lambda: 0)
    monkeypatch.setenv("PENGUIN_BURNER_AUTO_UV3", "1")

    command = commands.scan_command()

    assert "--auto-uv3" in command


def test_ui_scan_command_can_enable_auto_uv3_from_options(monkeypatch) -> None:
    monkeypatch.setattr(commands.os, "geteuid", lambda: 0)

    command = commands.scan_command({"auto_uv3": True})

    assert "--auto-uv3" in command
