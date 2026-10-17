"""Commands and desktop guidance for overlay global shortcuts."""

from __future__ import annotations

import os
import shlex
import shutil

from common.flatpak_wrappers import running_in_flatpak

SHORTCUT_ACTIONS = (
    ("Toggle overlay", "--overlay-toggle"),
    ("Next detail level", "--overlay-detail-next"),
)


def shortcut_command(action: str) -> str:
    if action not in {flag for _, flag in SHORTCUT_ACTIONS}:
        raise ValueError(f"Unknown overlay shortcut action: {action}")
    if running_in_flatpak():
        command = [
            "flatpak", "run", "--command=penguin-burner-cli",
            "io.github.jpietek.PenguinBurner",
        ]
    else:
        command = [shutil.which("penguin-burner-cli") or "penguin-burner-cli"]
    return shlex.join([*command, action])


def shortcut_desktop() -> str:
    desktops = os.environ.get("XDG_CURRENT_DESKTOP", "").upper().split(":")
    if "KDE" in desktops:
        return "kde"
    if "GNOME" in desktops:
        return "gnome"
    return "other"


def shortcut_settings_command() -> list[str] | None:
    desktop = shortcut_desktop()
    if desktop == "kde":
        return ["/usr/bin/kcmshell6", "kcm_keys"]
    if desktop == "gnome":
        return ["/usr/bin/gnome-control-center", "keyboard"]
    return None


def shortcut_instructions() -> str:
    desktop = shortcut_desktop()
    if desktop == "kde":
        return (
            "In System Settings → Keyboard → Shortcuts, add PenguinBurner and "
            "assign a key to Toggle overlay or Next overlay detail level. "
            "You can also add a Command or Script and paste a command below."
        )
    if desktop == "gnome":
        return (
            "In Settings → Keyboard → View and Customize Shortcuts → Custom "
            "Shortcuts, add a shortcut, paste a command below and choose a key."
        )
    return (
        "Add a global shortcut in your desktop settings and paste a command "
        "below. On Sway use bindsym with exec; on Hyprland use bind with exec."
    )
