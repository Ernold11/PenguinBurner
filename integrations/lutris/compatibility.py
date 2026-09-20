"""Use Lutris's own Wine/Proton catalogue and per-game Wine version setting."""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from common.atomic_write import atomic_write_text
from integrations.launchers.compatibility import (
    CompatibilitySelection,
    CompatibilityTool,
)
from integrations.launchers.host_process import run_on_host

from .config_store import LutrisConfigError, read_game_config
from .library import InstalledLutrisGame
from .paths import runner_config_path

# Run in host Python: PenguinBurner's Flatpak does not contain Lutris's modules.
# These APIs enumerate tools; they do not install versions or launch a game.
_CATALOGUE = '''
import json
from lutris.util.wine.wine import get_installed_wine_versions, get_default_wine_version
print(json.dumps({'versions': get_installed_wine_versions(), 'default': get_default_wine_version()}))
'''


class LutrisCompatibility:
    def __init__(self, home: Path | None = None) -> None:
        self.home = home
        self.default = "Launcher default"

    def discover(self) -> tuple[CompatibilityTool, ...]:
        # An explicit home is an isolated library, not the host's Lutris install.
        if self.home is not None:
            return ()
        result = run_on_host(["/usr/bin/python3", "-c", _CATALOGUE], capture=True, timeout=10)
        if result is None or result.returncode:
            raise ValueError("Could not query Lutris's Wine versions. Install native Lutris, then Rescan.")
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise TypeError("Lutris returned an invalid compatibility catalogue.")
        versions = payload.get("versions")
        if not isinstance(versions, list) or not all(isinstance(v, str) for v in versions):
            raise ValueError("Lutris returned an invalid compatibility tool list.")
        self.default = self._label(str(payload.get("default") or "Launcher default"))
        return tuple(CompatibilityTool(value, self._label(value)) for value in sorted(set(versions)))

    @staticmethod
    def _label(value: str) -> str:
        return {"ge-proton": "GE-Proton Latest", "system": "System Wine"}.get(value, value)

    def selection(self, game: InstalledLutrisGame) -> CompatibilitySelection:
        if game.runner != "wine" or game.config_path is None:
            return CompatibilitySelection(supported=False)
        section = self._wine(game.config_path)
        value = str(section.get("version") or "")
        inherited = self._wine(runner_config_path("wine", self.home)).get("version")
        default = self._label(str(inherited)) if inherited and inherited != "ge-proton" else self.default
        # Lutris treats ge-proton as a request to continue resolving defaults.
        if value == "ge-proton" and inherited and inherited != "ge-proton":
            label = f"GE-Proton (Latest; currently inherits {default})"
        else:
            label = self._label(value)
        if value == "custom":
            label = f"Custom Wine ({section.get('custom_wine_path') or 'path unset'})"
        return CompatibilitySelection(value, label, f"Lutris default ({default})")

    @staticmethod
    def _wine(path: Path | None) -> dict:
        document = read_game_config(path, strict=True) if path is not None else {}
        section = document.get("wine", {})
        if not isinstance(section, dict):
            raise LutrisConfigError("Invalid Lutris Wine settings; fix them in Lutris first.")
        return section

    def write(self, game: InstalledLutrisGame, tool: CompatibilityTool | None) -> None:
        if game.config_path is None:
            raise ValueError("This game has no Lutris configuration.")
        document = read_game_config(game.config_path, strict=True)
        section = dict(self._wine(game.config_path))
        if tool is None:
            section.pop("version", None)
        else:
            section["version"] = tool.value
        if section:
            document["wine"] = section
        else:
            document.pop("wine", None)
        atomic_write_text(game.config_path, yaml.safe_dump(document, sort_keys=True), durable=True)
