"""Read-only Wine discovery, also executed inside Heroic's Flatpak via python -c.

Keep this module standard-library-only: the sandbox has no PenguinBurner install.
Paths and WineInstallation records follow Heroic's compatibility_layers.ts.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path


def discover(request: dict) -> list[dict]:
    root = Path(request["root"])
    home = Path(request["home"])
    document = json.loads((root / "config.json").read_text())
    if not isinstance(document, dict) or not isinstance(document.get("defaultSettings", {}), dict):
        raise TypeError("Invalid Heroic global settings.")
    defaults = document.get("defaultSettings", {})
    tools: dict[str, dict] = {}

    def add(binary: Path, name: str, kind: str) -> None:
        if not binary.is_file() or not os.access(binary, os.X_OK):
            return
        item = {"bin": str(binary), "name": name, "type": kind}
        if kind == "wine":
            for key, relative in (("wineserver", "wineserver"), ("lib", "../lib64"), ("lib32", "../lib")):
                candidate = binary.parent / relative
                item[key] = str(candidate) if candidate.exists() else ""
        tools[str(binary)] = item

    def scan(directory: Path, kind: str) -> None:
        if not directory.is_dir():
            return
        for path in sorted(directory.iterdir()):
            if kind == "proton" and path.name.startswith("UMU-Latest"):
                continue
            add(path / ("proton" if kind == "proton" else "bin/wine"), path.name, kind)

    scan(root / "tools/wine", "wine")
    scan(home / ".local/share/lutris/runners/wine", "wine")
    scan(root / "tools/proton", "proton")
    steam_roots = [Path(value) for value in request.get("steam_roots", [])]
    if defaults.get("defaultSteamPath"):
        steam_roots.append(Path(defaults["defaultSteamPath"]))
    for steam in steam_roots:
        scan(steam / "compatibilitytools.d", "proton")
        scan(steam / "root/compatibilitytools.d", "proton")
        if defaults.get("showValveProton"):
            scan(steam / "steamapps/common", "proton")
    custom = defaults.get("customWinePaths") or []
    if not isinstance(custom, list):
        raise TypeError("Invalid Heroic custom Wine paths.")
    for value in custom:
        if not isinstance(value, str) or not Path(value).is_absolute():
            continue
        binary = Path(value)
        kind = "proton" if binary.name == "proton" else "wine"
        add(binary, f"Custom {kind.title()} - {value}", kind)
    if request.get("system", True):
        binary = shutil.which("wine")
        if binary:
            add(Path(binary), "System Wine", "wine")
    return list(tools.values())


if __name__ == "__main__":
    print(json.dumps(discover(json.loads(sys.argv[1]))))
