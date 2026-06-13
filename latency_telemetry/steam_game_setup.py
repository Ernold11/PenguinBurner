from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import time

from .steam_launch_check import (
    PENGUIN_BURNER_WRAPPER,
    RE9_APP_ID,
    check_compat_tool,
    check_launch_options,
    default_localconfig_paths,
    default_steam_config_paths,
    _quoted_tokens,
)


DEFAULT_CACHYOS_COMPAT_TOOL = "Proton-CachyOS Latest"
EXCLUDED_INSTALLED_APP_NAME_PREFIXES = (
    "Proton ",
    "Steam Linux Runtime",
    "Steamworks Common Redistributables",
)
FF7_REBIRTH_APP_ID = "2909400"
FIRST_LIGHT_APP_ID = "3768760"
HL2_RTX_APP_ID = "2477290"
LEGO_BATMAN_APP_ID = "2215200"
CRASH_4_APP_ID = "1378990"
QUAKE_II_RTX_APP_ID = "1089130"
GENERIC_REQUIRED_TOKENS = (PENGUIN_BURNER_WRAPPER,)

# The experimental in-game-latency toggle. Present in the launch line, it tells
# the PENGUIN_BURNER wrapper to enable stock Proton dxvk-nvapi trace logging;
# the bridge service then drains the in-memory trace FIFO, pairs sim->present
# markers by frame id, and feeds the receiver -> overlay as latency_ms. No
# custom DLL, works on any Proton; cost is the heavier trace logging volume
# (only while enabled). PB_INGAME_LATENCY is the short alias the wrapper
# accepts (alongside PENGUIN_BURNER_INGAME_LATENCY).
INGAME_LATENCY_TOKENS = ("PB_INGAME_LATENCY=1",)
OVERLAY_TOKENS = ("PENGUIN_BURNER_OVERLAY=1", "PB_OVERLAY=1")


@dataclass(frozen=True)
class SteamGamePreset:
    key: str
    name: str
    app_id: str
    disable_wine_detection: bool = True


RE9_PRESET = SteamGamePreset(
    key="re9",
    name="Resident Evil Requiem",
    app_id=RE9_APP_ID,
)
FF7_REBIRTH_PRESET = SteamGamePreset(
    key="ff7-rebirth",
    name="FINAL FANTASY VII REBIRTH",
    app_id=FF7_REBIRTH_APP_ID,
)
FIRST_LIGHT_PRESET = SteamGamePreset(
    key="007-first-light",
    name="007 First Light",
    app_id=FIRST_LIGHT_APP_ID,
)
HL2_RTX_PRESET = SteamGamePreset(
    key="hl2-rtx",
    name="Half-Life 2 RTX",
    app_id=HL2_RTX_APP_ID,
)
LEGO_BATMAN_PRESET = SteamGamePreset(
    key="lego-batman",
    name="LEGO Batman: Legacy of the Dark Knight",
    app_id=LEGO_BATMAN_APP_ID,
)
CRASH_4_PRESET = SteamGamePreset(
    key="crash-4",
    name="Crash Bandicoot 4: It's About Time",
    app_id=CRASH_4_APP_ID,
)
QUAKE_II_RTX_PRESET = SteamGamePreset(
    key="quake-ii-rtx",
    name="Quake II RTX",
    app_id=QUAKE_II_RTX_APP_ID,
)
STEAM_GAME_PRESETS = (
    RE9_PRESET,
    FF7_REBIRTH_PRESET,
    FIRST_LIGHT_PRESET,
    HL2_RTX_PRESET,
    LEGO_BATMAN_PRESET,
    CRASH_4_PRESET,
    QUAKE_II_RTX_PRESET,
)
STEAM_GAME_PRESETS_BY_KEY = {preset.key: preset for preset in STEAM_GAME_PRESETS}


@dataclass(frozen=True)
class CachyosCompatTool:
    name: str
    version: str
    timestamp: int
    path: Path


def build_game_launch_options(
    preset: SteamGamePreset,
    *,
    ingame_latency: bool = False,
    overlay: bool = False,
) -> str:
    # The wrapper expands to the full Vulkan/NVAPI env. The optional
    # PENGUIN_BURNER_INGAME_LATENCY toggle tells the wrapper to also enable
    # dxvk-nvapi trace logging (the heavy part) so the marker bridge can derive
    # in-game latency; without it the wrapper stays trace-free.
    tokens: list[str] = []
    if overlay:
        tokens.extend(OVERLAY_TOKENS)
    if ingame_latency:
        tokens.extend(INGAME_LATENCY_TOKENS)
    tokens.append(PENGUIN_BURNER_WRAPPER)
    command = "%command%"
    if preset.disable_wine_detection:
        command += " /WineDetectionEnabled:False"
    tokens.append(command)
    return " ".join(tokens)


class SteamConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class SteamSetupResult:
    game_name: str
    app_id: str
    localconfig_path: Path
    steam_config_path: Path
    launch_options: str
    compat_tool: str
    launch_options_changed: bool
    compat_tool_changed: bool
    localconfig_backup: Path | None
    steam_config_backup: Path | None
    dry_run: bool

    def format_text(self) -> str:
        return "\n".join(
            [
                f"game={self.game_name}",
                f"app_id={self.app_id}",
                f"localconfig={self.localconfig_path}",
                f"steam_config={self.steam_config_path}",
                f"dry_run={self.dry_run}",
                f"launch_options_changed={self.launch_options_changed}",
                f"compat_tool_changed={self.compat_tool_changed}",
                f"localconfig_backup={self.localconfig_backup or 'not-written'}",
                f"steam_config_backup={self.steam_config_backup or 'not-written'}",
                f"launch_options={self.launch_options}",
                f"compat_tool={self.compat_tool}",
            ]
        )


def running_steam_processes() -> tuple[str, ...]:
    result = subprocess.run(
        [
            "pgrep",
            "-af",
            (
                "steam|steamwebhelper|wineserver|SteamLaunch|re9|Resident|"
                "BIOHAZARD|ff7rebirth|007 First Light|engine.exe|hl2|"
                "Half-Life 2|LEGO Batman|CrashBandicoot4|Crash Bandicoot|"
                "quake2rtx|Quake II RTX"
            ),
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if result.returncode not in (0, 1):
        return ()
    lines = []
    for line in result.stdout.splitlines():
        if (
            "penguin-burner-steam-game-setup" in line
            or "latency_telemetry.steam_game_setup" in line
        ):
            continue
        lines.append(line)
    return tuple(lines)


def wait_for_steam_exit(
    *,
    timeout_s: float | None = None,
    poll_interval_s: float = 2.0,
) -> tuple[str, ...]:
    start = time.monotonic()
    while True:
        processes = running_steam_processes()
        if not processes:
            return ()
        if timeout_s is not None and time.monotonic() - start >= timeout_s:
            return processes
        time.sleep(poll_interval_s)


def _quote_vdf_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _line_indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _localconfig_has_app(text: str, app_id: str) -> bool:
    return any(_quoted_tokens(line) == [app_id] for line in text.splitlines())


def _find_path_with_app_block(paths: list[Path], app_id: str) -> Path:
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _localconfig_has_app(text, app_id):
            return path
    raise SteamConfigError(f"Could not find Steam app {app_id} in localconfig.")


def _find_path_with_compat_tool_block(paths: list[Path]) -> Path:
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(
            _quoted_tokens(line) == ["CompatToolMapping"]
            for line in text.splitlines()
        ):
            return path
    raise SteamConfigError("Could not find Steam CompatToolMapping block.")


def set_launch_options_in_localconfig(
    text: str,
    *,
    app_id: str,
    launch_options: str,
) -> tuple[str, bool]:
    lines = text.splitlines()
    quoted_launch_options = _quote_vdf_value(launch_options)
    for index, line in enumerate(lines):
        if _quoted_tokens(line) != [app_id]:
            continue

        depth = 0
        entered_block = False
        insert_index: int | None = None
        fallback_indent = f"{_line_indent(line)}\t"
        for block_index, block_line in enumerate(lines[index + 1 :], start=index + 1):
            stripped = block_line.strip()
            if stripped == "{":
                depth += 1
                entered_block = True
                continue
            if stripped == "}":
                if not entered_block:
                    break
                depth -= 1
                if depth <= 0:
                    insert_index = block_index
                    break
                continue
            if not entered_block or depth <= 0:
                continue

            tokens = _quoted_tokens(block_line)
            if len(tokens) >= 2 and tokens[0] == "LaunchOptions":
                replacement = (
                    f'{_line_indent(block_line)}"LaunchOptions"\t\t'
                    f'"{quoted_launch_options}"'
                )
                if block_line == replacement:
                    return text, False
                lines[block_index] = replacement
                return "\n".join(lines) + "\n", True

        if insert_index is not None:
            lines.insert(
                insert_index,
                f'{fallback_indent}"LaunchOptions"\t\t"{quoted_launch_options}"',
            )
            return "\n".join(lines) + "\n", True

    inserted = _insert_app_launch_options_in_localconfig(
        lines,
        app_id=app_id,
        quoted_launch_options=quoted_launch_options,
    )
    if inserted:
        return "\n".join(lines) + "\n", True

    raise SteamConfigError(f"Could not update LaunchOptions for Steam app {app_id}.")


def _insert_app_launch_options_in_localconfig(
    lines: list[str],
    *,
    app_id: str,
    quoted_launch_options: str,
) -> bool:
    for index, line in enumerate(lines):
        if _quoted_tokens(line) != ["apps"]:
            continue

        depth = 0
        entered_block = False
        insert_index: int | None = None
        app_indent = f"{_line_indent(line)}\t"
        for block_index, block_line in enumerate(lines[index + 1 :], start=index + 1):
            stripped = block_line.strip()
            if stripped == "{":
                depth += 1
                entered_block = True
                continue
            if stripped == "}":
                if not entered_block:
                    break
                if depth == 1:
                    insert_index = block_index
                depth -= 1
                if depth <= 0:
                    break
                continue
            if not entered_block or depth != 1:
                continue
            tokens = _quoted_tokens(block_line)
            if len(tokens) == 1:
                app_indent = _line_indent(block_line)

        if insert_index is None:
            return False

        block = [
            f'{app_indent}"{app_id}"',
            f"{app_indent}{{",
            f'{app_indent}\t"LaunchOptions"\t\t"{quoted_launch_options}"',
            f"{app_indent}}}",
        ]
        lines[insert_index:insert_index] = block
        return True

    return False


def set_compat_tool_in_config(
    text: str,
    *,
    app_id: str,
    compat_tool: str,
) -> tuple[str, bool]:
    lines = text.splitlines()
    quoted_compat_tool = _quote_vdf_value(compat_tool)
    for index, line in enumerate(lines):
        if _quoted_tokens(line) != ["CompatToolMapping"]:
            continue

        depth = 0
        entered_block = False
        mapping_insert_index: int | None = None
        app_indent = f"{_line_indent(line)}\t"
        for block_index, block_line in enumerate(lines[index + 1 :], start=index + 1):
            stripped = block_line.strip()
            if stripped == "{":
                depth += 1
                entered_block = True
                continue
            if stripped == "}":
                if not entered_block:
                    break
                if depth == 1:
                    mapping_insert_index = block_index
                depth -= 1
                if depth <= 0:
                    break
                continue
            if not entered_block or depth != 1:
                continue
            tokens = _quoted_tokens(block_line)
            if len(tokens) == 1:
                app_indent = _line_indent(block_line)
            if tokens != [app_id]:
                continue

            app_depth = 0
            app_entered_block = False
            insert_index: int | None = None
            fallback_indent = f"{_line_indent(block_line)}\t"
            for app_index, app_line in enumerate(
                lines[block_index + 1 :], start=block_index + 1
            ):
                app_stripped = app_line.strip()
                if app_stripped == "{":
                    app_depth += 1
                    app_entered_block = True
                    continue
                if app_stripped == "}":
                    if not app_entered_block:
                        break
                    app_depth -= 1
                    if app_depth <= 0:
                        insert_index = app_index
                        break
                    continue
                if not app_entered_block or app_depth <= 0:
                    continue

                tokens = _quoted_tokens(app_line)
                if len(tokens) >= 2 and tokens[0] == "name":
                    replacement = (
                        f'{_line_indent(app_line)}"name"\t\t"{quoted_compat_tool}"'
                    )
                    if app_line == replacement:
                        return text, False
                    lines[app_index] = replacement
                    return "\n".join(lines) + "\n", True

            if insert_index is not None:
                lines.insert(
                    insert_index,
                    f'{fallback_indent}"name"\t\t"{quoted_compat_tool}"',
                )
                return "\n".join(lines) + "\n", True

        if mapping_insert_index is not None:
            block = [
                f'{app_indent}"{app_id}"',
                f"{app_indent}{{",
                f'{app_indent}\t"name"\t\t"{quoted_compat_tool}"',
                f'{app_indent}\t"config"\t\t""',
                f'{app_indent}\t"priority"\t\t"250"',
                f"{app_indent}}}",
            ]
            lines[mapping_insert_index:mapping_insert_index] = block
            return "\n".join(lines) + "\n", True

    raise SteamConfigError(f"Could not update CompatToolMapping for Steam app {app_id}.")


def installed_cachyos_compat_tools(home: Path | None = None) -> tuple[CachyosCompatTool, ...]:
    home = Path.home() if home is None else home
    roots = [
        home / ".local" / "share" / "Steam" / "compatibilitytools.d",
        home / ".steam" / "root" / "compatibilitytools.d",
        home / ".steam" / "steam" / "compatibilitytools.d",
    ]
    seen_roots: set[Path] = set()
    tools: list[CachyosCompatTool] = []
    for root in roots:
        try:
            resolved_root = root.resolve()
        except OSError:
            resolved_root = root
        if resolved_root in seen_roots or not root.is_dir():
            continue
        seen_roots.add(resolved_root)
        for path in sorted(child for child in root.iterdir() if child.is_dir()):
            tool = _cachyos_compat_tool_from_path(path)
            if tool is not None:
                tools.append(tool)

    by_name: dict[str, CachyosCompatTool] = {}
    for tool in tools:
        current = by_name.get(tool.name)
        if current is None or (tool.timestamp, tool.version) > (
            current.timestamp,
            current.version,
        ):
            by_name[tool.name] = tool
    return tuple(
        sorted(
            by_name.values(),
            key=lambda tool: (tool.timestamp, tool.version, tool.name),
        )
    )


def preferred_cachyos_compat_tool_name(home: Path | None = None) -> str:
    tools = installed_cachyos_compat_tools(home)
    if not tools:
        return DEFAULT_CACHYOS_COMPAT_TOOL
    return tools[-1].name


def installed_steam_game_presets(home: Path | None = None) -> tuple[SteamGamePreset, ...]:
    home = Path.home() if home is None else home
    presets: list[SteamGamePreset] = []
    seen_app_ids: set[str] = set()
    for steamapps_dir in default_steamapps_dirs(home):
        for manifest in sorted(steamapps_dir.glob("appmanifest_*.acf")):
            data = _manifest_fields(manifest)
            app_id = data.get("appid", "").strip()
            name = data.get("name", "").strip()
            if not app_id or not name or app_id in seen_app_ids:
                continue
            if _is_excluded_installed_app(name):
                continue
            seen_app_ids.add(app_id)
            presets.append(
                SteamGamePreset(
                    key=f"installed-{app_id}",
                    name=name,
                    app_id=app_id,
                )
            )
    return tuple(presets)


def default_steamapps_dirs(home: Path | None = None) -> tuple[Path, ...]:
    home = Path.home() if home is None else home
    candidates = [
        home / ".local" / "share" / "Steam" / "steamapps",
        home / ".steam" / "root" / "steamapps",
        home / ".steam" / "steam" / "steamapps",
    ]
    for base in tuple(candidates):
        candidates.extend(_library_steamapps_dirs(base / "libraryfolders.vdf"))

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen or not path.is_dir():
            continue
        seen.add(resolved)
        unique.append(path)
    return tuple(unique)


def _library_steamapps_dirs(path: Path) -> tuple[Path, ...]:
    data = _manifest_fields(path)
    paths = []
    for value in data.get_all("path"):
        if value:
            paths.append(Path(value).expanduser() / "steamapps")
    return tuple(paths)


def _manifest_fields(path: Path) -> "_ManifestFields":
    fields = _ManifestFields()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return fields
    for line in lines:
        tokens = _quoted_tokens(line)
        if len(tokens) >= 2:
            fields.add(tokens[0], tokens[1])
    return fields


class _ManifestFields(dict[str, str]):
    def __init__(self) -> None:
        super().__init__()
        self._all: dict[str, list[str]] = {}

    def add(self, key: str, value: str) -> None:
        self.setdefault(key, value)
        self._all.setdefault(key, []).append(value)

    def get_all(self, key: str) -> tuple[str, ...]:
        return tuple(self._all.get(key, ()))


def _is_excluded_installed_app(name: str) -> bool:
    return name.startswith(EXCLUDED_INSTALLED_APP_NAME_PREFIXES)


def _cachyos_compat_tool_from_path(path: Path) -> CachyosCompatTool | None:
    try:
        version_text = (path / "version").read_text(
            encoding="utf-8", errors="replace"
        ).strip()
    except OSError:
        return None
    if "cachyos" not in version_text.lower():
        return None
    parts = version_text.split(maxsplit=1)
    try:
        timestamp = int(parts[0])
    except (IndexError, ValueError):
        timestamp = 0
    version = parts[1] if len(parts) > 1 else version_text
    name = _compat_tool_name_from_manifest(path / "compatibilitytool.vdf") or path.name
    return CachyosCompatTool(
        name=name,
        version=version,
        timestamp=timestamp,
        path=path,
    )


def _compat_tool_name_from_manifest(path: Path) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for index, line in enumerate(lines):
        if _quoted_tokens(line) != ["compat_tools"]:
            continue
        depth = 0
        entered_block = False
        for block_line in lines[index + 1 :]:
            stripped = block_line.strip()
            if stripped == "{":
                depth += 1
                entered_block = True
                continue
            if stripped == "}":
                if not entered_block:
                    break
                depth -= 1
                if depth <= 0:
                    break
                continue
            if entered_block and depth == 1:
                tokens = _quoted_tokens(block_line)
                if len(tokens) == 1:
                    return tokens[0]
    return None


def _backup_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.pburn-bak")


def _write_with_backup(path: Path, text: str) -> Path:
    backup = _backup_path(path)
    if not backup.exists():
        backup.write_text(
            path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8"
        )
    path.write_text(text, encoding="utf-8")
    return backup


_REPO_ROOT = Path(__file__).resolve().parent.parent
# In-memory trace FIFO the wrapper writes to and the bridge drains -- matches
# penguin_burner_overlay.launcher.trace_fifo_path. No on-disk Proton log.
NVAPI_TRACE_FIFO_PATH = Path.home() / ".cache" / "penguin-burner" / "nvapi-trace.fifo"
_BRIDGE_SERVICE_NAME = "pb-latency-bridge.service"


def _bridge_service_unit() -> str:
    return (
        "[Unit]\n"
        "Description=PenguinBurner in-game latency bridge (dxvk-nvapi trace)\n\n"
        "[Service]\n"
        f"WorkingDirectory={_REPO_ROOT}\n"
        "ExecStart=/usr/bin/python3 -m latency_telemetry.nvapi_marker_bridge "
        f"--log {NVAPI_TRACE_FIFO_PATH}\n"
        "Restart=always\n"
        "RestartSec=2\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _ensure_trace_fifo() -> None:
    NVAPI_TRACE_FIFO_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not NVAPI_TRACE_FIFO_PATH.exists():
        os.mkfifo(NVAPI_TRACE_FIFO_PATH, 0o600)


def _systemctl_user(*args: str) -> None:
    subprocess.run(
        ["systemctl", "--user", *args],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def install_ingame_latency_bridge_service() -> Path:
    """Install + enable the user service that runs the marker bridge."""
    _ensure_trace_fifo()
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / _BRIDGE_SERVICE_NAME
    unit_path.write_text(_bridge_service_unit(), encoding="utf-8")
    _systemctl_user("daemon-reload")
    _systemctl_user("enable", "--now", _BRIDGE_SERVICE_NAME)
    _systemctl_user("restart", _BRIDGE_SERVICE_NAME)
    return unit_path


def remove_ingame_latency_bridge_service() -> None:
    _systemctl_user("disable", "--now", _BRIDGE_SERVICE_NAME)
    unit_path = Path.home() / ".config" / "systemd" / "user" / _BRIDGE_SERVICE_NAME
    try:
        unit_path.unlink()
    except OSError:
        pass
    _systemctl_user("daemon-reload")


def apply_steam_game_setup(
    preset: SteamGamePreset,
    *,
    localconfig_path: Path | None = None,
    steam_config_path: Path | None = None,
    dry_run: bool = False,
    check_running: bool = True,
    wait: bool = False,
    wait_timeout_s: float | None = None,
    poll_interval_s: float = 2.0,
    ingame_latency: bool = False,
    overlay: bool = False,
    compat_tool: str | None = None,
    manage_bridge_service: bool = True,
) -> SteamSetupResult:
    if check_running:
        processes = (
            wait_for_steam_exit(
                timeout_s=wait_timeout_s,
                poll_interval_s=poll_interval_s,
            )
            if wait
            else running_steam_processes()
        )
        if processes:
            preview = "\n".join(processes[:8])
            raise SteamConfigError(
                "Steam or a Wine game process is still running; close Steam before "
                f"editing config files.\n{preview}"
            )

    localconfig_path = localconfig_path or _find_path_with_app_block(
        default_localconfig_paths(), preset.app_id
    )
    steam_config_path = steam_config_path or _find_path_with_compat_tool_block(
        default_steam_config_paths()
    )
    compat_tool = compat_tool or preferred_cachyos_compat_tool_name()

    localconfig_text = localconfig_path.read_text(encoding="utf-8", errors="replace")
    steam_config_text = steam_config_path.read_text(encoding="utf-8", errors="replace")

    launch_options = build_game_launch_options(
        preset,
        ingame_latency=ingame_latency,
        overlay=overlay,
    )
    new_localconfig_text, launch_changed = set_launch_options_in_localconfig(
        localconfig_text,
        app_id=preset.app_id,
        launch_options=launch_options,
    )
    new_steam_config_text, compat_changed = set_compat_tool_in_config(
        steam_config_text,
        app_id=preset.app_id,
        compat_tool=compat_tool,
    )

    localconfig_backup = None
    steam_config_backup = None
    if not dry_run:
        if launch_changed:
            localconfig_backup = _write_with_backup(localconfig_path, new_localconfig_text)
        if compat_changed:
            steam_config_backup = _write_with_backup(steam_config_path, new_steam_config_text)

        launch_check = check_launch_options(
            app_id=preset.app_id,
            required_tokens=GENERIC_REQUIRED_TOKENS,
            config_paths=[localconfig_path],
        )
        compat_check = check_compat_tool(
            app_id=preset.app_id,
            expected_tool=compat_tool,
            config_paths=[steam_config_path],
        )
        if not launch_check.ok or not compat_check.ok:
            raise SteamConfigError(
                f"Steam setup for {preset.name} did not verify after writing."
            )

        # Manage the in-game latency bridge service to match the requested mode.
        if manage_bridge_service:
            if ingame_latency:
                install_ingame_latency_bridge_service()
            else:
                remove_ingame_latency_bridge_service()

    return SteamSetupResult(
        game_name=preset.name,
        app_id=preset.app_id,
        localconfig_path=localconfig_path,
        steam_config_path=steam_config_path,
        launch_options=launch_options,
        compat_tool=compat_tool,
        launch_options_changed=launch_changed,
        compat_tool_changed=compat_changed,
        localconfig_backup=localconfig_backup,
        steam_config_backup=steam_config_backup,
        dry_run=dry_run,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Apply PenguinBurner Steam launch configuration for supported games."
        )
    )
    parser.add_argument(
        "--game",
        action="append",
        choices=tuple(STEAM_GAME_PRESETS_BY_KEY),
        default=None,
        help="Game preset to update. May be repeated. Defaults to all supported games.",
    )
    parser.add_argument(
        "--installed",
        action="store_true",
        help=(
            "Update every installed non-tool Steam app discovered from app manifests."
        ),
    )
    parser.add_argument("--localconfig", type=Path, default=None)
    parser.add_argument("--steam-config", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--wait",
        action="store_true",
        help="Wait until Steam/Wine game processes exit before writing.",
    )
    parser.add_argument(
        "--ignore-running",
        action="store_true",
        help="Edit Steam config even if Steam or a Wine game process is running.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Maximum seconds to wait with --wait. Defaults to no timeout.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="Seconds between process checks with --wait.",
    )
    parser.add_argument(
        "--experimental-ingame-latency",
        action="store_true",
        help=(
            "Enable experimental in-game (under frame generation) latency: adds "
            "a wrapper toggle for stock dxvk-nvapi trace logging and installs "
            "the marker-bridge user service. Heavier trace logging; no custom DLL."
        ),
    )
    parser.add_argument(
        "--overlay",
        action="store_true",
        help=(
            "Explicitly show the native PenguinBurner in-game overlay by adding "
            "PB_OVERLAY=1 to the launch options. The wrapper default is hidden."
        ),
    )
    parser.add_argument(
        "--compat-tool",
        default=None,
        help=(
            "Steam compatibility tool name. Defaults to the newest installed "
            "CachyOS Proton tool."
        ),
    )
    args = parser.parse_args(argv)

    try:
        results = []
        if args.installed and args.game:
            raise SteamConfigError("--installed cannot be combined with --game.")
        if args.installed:
            presets = installed_steam_game_presets()
        else:
            game_keys = args.game or [preset.key for preset in STEAM_GAME_PRESETS]
            presets = [STEAM_GAME_PRESETS_BY_KEY[game_key] for game_key in game_keys]
        for preset in presets:
            results.append(
                apply_steam_game_setup(
                    preset,
                    localconfig_path=args.localconfig,
                    steam_config_path=args.steam_config,
                    dry_run=args.dry_run,
                    check_running=not args.ignore_running,
                    wait=args.wait,
                    wait_timeout_s=args.timeout,
                    poll_interval_s=args.poll_interval,
                    ingame_latency=args.experimental_ingame_latency,
                    overlay=args.overlay,
                    compat_tool=args.compat_tool,
                    manage_bridge_service=False,
                )
            )
        if not args.dry_run:
            if args.experimental_ingame_latency:
                install_ingame_latency_bridge_service()
            else:
                remove_ingame_latency_bridge_service()
    except SteamConfigError as exc:
        print(f"error={exc}")
        return 1

    print("\n\n".join(result.format_text() for result in results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
