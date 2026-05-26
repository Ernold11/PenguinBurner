from __future__ import annotations

import ui.commands as commands


def test_ui_scan_command_uses_auto_uv_voltage_scan(monkeypatch) -> None:
    monkeypatch.setattr(commands.os, "geteuid", lambda: 0)

    command = commands.scan_command()

    assert "--auto-uv-voltage-scan" in command


def test_ui_scan_command_ignores_removed_legacy_option(monkeypatch) -> None:
    monkeypatch.setattr(commands.os, "geteuid", lambda: 0)

    command = commands.scan_command({"auto_uv": True})

    assert "--auto-uv-voltage-scan" in command
