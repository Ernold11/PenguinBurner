"""Heroic WineInstallation records, with discovery inside the selected sandbox."""
from __future__ import annotations

import json
from pathlib import Path

from common.atomic_write import atomic_write_text
from integrations.launchers.compatibility import (
    CompatibilitySelection,
    CompatibilityTool,
)
from integrations.launchers.host_process import run_on_host
from overlay.telemetry.steam_game_setup import default_steamapps_dirs

from . import compat_probe
from .config_store import HeroicConfigError, read_game_config
from .flatpak import APP_ID, uses_flatpak
from .library import InstalledHeroicGame
from .paths import game_config_path, global_config_path, heroic_config_root


def wine_settings(app_name: str, home: Path | None = None) -> tuple[dict, dict]:
    """Explicit game settings and inherited defaults, rejecting damaged configs."""
    document = read_game_config(app_name, home, strict=True)
    settings = document.get(app_name, {})
    global_document = json.loads(global_config_path(home).read_text())
    if not isinstance(global_document, dict):
        raise HeroicConfigError("Invalid Heroic global settings.")
    defaults = global_document.get("defaultSettings", {})
    if not isinstance(settings, dict) or not isinstance(defaults, dict):
        raise HeroicConfigError("Invalid Heroic settings; fix them in Heroic first.")
    return settings, defaults


class HeroicCompatibility:
    def __init__(self, home: Path | None = None) -> None:
        self.home = home

    def discover(self) -> tuple[CompatibilityTool, ...]:
        home = self.home or Path.home()
        request = {
            "root": str(heroic_config_root(self.home)), "home": str(home),
            "steam_roots": [str(path.parent) for path in default_steamapps_dirs(self.home)],
            "system": self.home is None,
        }
        if self.home is not None:
            records = compat_probe.discover(request)
        else:
            script = Path(compat_probe.__file__).read_text()
            command = (["flatpak", "run", "--command=python3", APP_ID]
                       if uses_flatpak(self.home) else ["/usr/bin/python3"])
            result = run_on_host([*command, "-c", script, json.dumps(request)], capture=True, timeout=10)
            if result is None or result.returncode:
                raise HeroicConfigError("Could not discover Heroic's compatibility tools. Rescan to retry.")
            records = json.loads(result.stdout)
        if not isinstance(records, list):
            raise HeroicConfigError("Heroic returned an invalid compatibility tool list.")
        tools = []
        for item in records:
            if not isinstance(item, dict) or not all(isinstance(item.get(key), str) for key in ("bin", "name", "type")):
                raise HeroicConfigError("Heroic returned an invalid compatibility tool.")
            tools.append(CompatibilityTool(item["bin"], f"{item['name']} ({item['type']})", item))
        return tuple(tools)

    def selection(self, game: InstalledHeroicGame) -> CompatibilitySelection:
        if game.is_native:
            return CompatibilitySelection(supported=False)
        settings, defaults = wine_settings(game.game_id, self.home)
        current = settings.get("wineVersion") or {}
        inherited = defaults.get("wineVersion") or {}
        if not isinstance(current, dict) or not isinstance(inherited, dict):
            raise HeroicConfigError("Invalid Heroic wineVersion setting.")
        return CompatibilitySelection(
            str(current.get("bin") or ""), str(current.get("name") or ""),
            f"Heroic default ({inherited.get('name') or 'automatic'})",
        )

    def write(self, game: InstalledHeroicGame, tool: CompatibilityTool | None) -> None:
        path = game_config_path(game.game_id, self.home)
        if path is None:
            raise HeroicConfigError("Invalid Heroic game id.")
        document = read_game_config(game.game_id, self.home, strict=True)
        settings = document.get(game.game_id, {})
        if not isinstance(settings, dict):
            raise HeroicConfigError("Invalid Heroic game settings.")
        if tool is None:
            settings.pop("wineVersion", None)
        else:
            settings["wineVersion"] = dict(tool.payload)
        document[game.game_id] = settings
        document.setdefault("version", "v0")
        atomic_write_text(path, json.dumps(document, indent=2) + "\n", durable=True)
