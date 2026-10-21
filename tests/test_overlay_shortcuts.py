from __future__ import annotations

import configparser
import shlex
import subprocess
import time
from pathlib import Path

import pytest
from PySide6 import QtCore, QtWidgets

from cli.arguments import parse_arguments
from overlay import shortcuts
from overlay.control import cycle_overlay_detail
from overlay.state import write_overlay_override
from ui.components.overlay_config import OverlayConfigPanel
from ui.dialogs import overlay_shortcuts


def test_native_shortcut_quotes_the_installed_executable(monkeypatch):
    monkeypatch.setattr(shortcuts, "running_in_flatpak", lambda: False)
    monkeypatch.setattr(shortcuts.shutil, "which", lambda _: "/home/test user/bin/pburn-cli")
    for _, flag in shortcuts.SHORTCUT_ACTIONS:
        assert shlex.split(shortcuts.shortcut_command(flag)) == [
            "/home/test user/bin/pburn-cli", flag,
        ]


def test_flatpak_shortcut_targets_the_host_install(monkeypatch):
    monkeypatch.setattr(shortcuts, "running_in_flatpak", lambda: True)
    assert shlex.split(shortcuts.shortcut_command("--overlay-toggle")) == [
        "flatpak", "run", "--command=penguin-burner-cli",
        "io.github.jpietek.PenguinBurner", "--overlay-toggle",
    ]


@pytest.mark.parametrize(
    "desktop, expected",
    [
        ("KDE", ["/usr/bin/kcmshell6", "kcm_keys"]),
        ("GNOME-Classic:GNOME", ["/usr/bin/gnome-control-center", "keyboard"]),
        ("sway", None),
    ],
)
def test_settings_target_matches_the_desktop(monkeypatch, desktop, expected):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", desktop)
    assert shortcuts.shortcut_settings_command() == expected


def test_overlay_panel_opens_shortcut_setup_without_changing_settings(qtbot, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    launched = []
    monkeypatch.setattr(overlay_shortcuts, "start_on_host", lambda command: launched.append(command) or True)
    config = tmp_path / "overlay.toml"
    panel = OverlayConfigPanel(QtCore=QtCore, QtWidgets=QtWidgets, config_path=config)
    qtbot.addWidget(panel.widget)
    panel.widget.show()
    panel.shortcuts_button.click()
    dialog = panel.widget.findChild(QtWidgets.QDialog, "overlayShortcutsDialog")
    assert dialog is not None and dialog.isVisible()
    assert not config.exists()
    assert not launched
    command = dialog.findChild(QtWidgets.QLineEdit, "overlayShortcutCommand0")
    dialog.findChild(QtWidgets.QPushButton, "overlayShortcutCopy0").click()
    assert QtWidgets.QApplication.clipboard().text() == command.text()
    assert shlex.split(command.text())[-1] == "--overlay-toggle"
    dialog.findChild(QtWidgets.QPushButton, "overlayShortcutSettings").click()
    assert launched == [["/usr/bin/kcmshell6", "kcm_keys"]]
    dialog.close()


def test_gui_follows_detail_changes_made_by_a_shortcut(qtbot, tmp_path):
    config = tmp_path / "overlay.toml"
    panel = OverlayConfigPanel(QtCore=QtCore, QtWidgets=QtWidgets, config_path=config)
    qtbot.addWidget(panel.widget)
    before = panel.config
    cycle_overlay_detail(config)
    saved_bytes = config.read_bytes()
    panel.refresh_preview()
    assert panel.config != before
    assert panel.detail_combo.currentText() == "Small"
    assert {key for key, box in panel.item_checkboxes.items() if box.isChecked()} == set(
        panel.config.enabled_item_ids
    )
    assert config.read_bytes() == saved_bytes


def test_native_visibility_reacts_before_the_old_one_second_poll(native_overlay_probe, tmp_path):
    override = tmp_path / "overlay-override"
    assert write_overlay_override(True, override)
    import os

    env = dict(os.environ, PENGUIN_BURNER_OVERLAY_OVERRIDE=str(override), PB_OVERLAY="1")
    with subprocess.Popen(
        [str(native_overlay_probe), "--visibility-watch"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env,
    ) as process:
        assert process.stdin is not None and process.stdout is not None
        try:
            for visible in [True, False, True]:
                assert write_overlay_override(visible, override)
                time.sleep(0.1)
                process.stdin.write("tick\n")
                process.stdin.flush()
                assert process.stdout.readline().strip() == str(int(visible))
        finally:
            process.stdin.close()
            process.wait(timeout=5)


@pytest.mark.parametrize("package", ["linux", "flatpak"])
def test_desktop_shortcut_actions_route_to_supported_cli_commands(package):
    entry = configparser.ConfigParser(interpolation=None)
    entry.read(Path("packaging") / package / "io.github.jpietek.PenguinBurner.desktop")
    actions = entry["Desktop Entry"]["Actions"].strip(";").split(";")
    assert actions == ["ToggleOverlay", "NextOverlayDetail"]
    for action, (_, flag) in zip(actions, shortcuts.SHORTCUT_ACTIONS, strict=True):
        command = shlex.split(entry[f"Desktop Action {action}"]["Exec"])
        assert command == ["penguin-burner-cli", flag]
        args = parse_arguments(command[1:])
        assert getattr(args, flag.removeprefix("--").replace("-", "_")) is True
