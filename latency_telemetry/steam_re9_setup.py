from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import subprocess
import time

from .steam_launch_check import (
    RE9_APP_ID,
    RE9_REQUIRED_TOKENS,
    check_compat_tool,
    check_launch_options,
    default_localconfig_paths,
    default_steam_config_paths,
    launch_options_from_localconfig,
    compat_tool_from_config,
    _quoted_tokens,
)


RE9_PRESENT_COMPAT_TOOL = "Proton-CachyOS Latest"
RE9_PRESENT_EXTRA_TOKENS = (
    "PROTON_ENABLE_NVAPI=1",
    "PROTON_HIDE_NVIDIA_GPU=0",
    "DXVK_NVAPI_VKREFLEX=1",
)
RE9_PRESENT_LAUNCH_OPTIONS = (
    "PENGUIN_BURNER_LATENCY_SOCKET=/run/user/1000/penguin-burner/latency.sock "
    "VK_ADD_IMPLICIT_LAYER_PATH=/home/jp/PenguinBurner/native/latency_layer/build:/home/jp/PenguinBurner/third_party/dxvk-nvapi/build.layer "
    "VK_LOADER_LAYERS_ENABLE=VK_LAYER_PENGUINBURNER_latency,VK_LAYER_DXVK_NVAPI_reflex "
    "PENGUIN_BURNER_LATENCY_LAYER=1 "
    "PROTON_ENABLE_NVAPI=1 "
    "PROTON_HIDE_NVIDIA_GPU=0 "
    "DXVK_NVAPI_VKREFLEX=1 "
    "gamemoderun %command% /WineDetectionEnabled:False"
)

RE9_PATCHED_COMPAT_TOOL = RE9_PRESENT_COMPAT_TOOL
RE9_PATCHED_EXTRA_TOKENS = RE9_PRESENT_EXTRA_TOKENS
RE9_PATCHED_LAUNCH_OPTIONS = RE9_PRESENT_LAUNCH_OPTIONS


class SteamConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class SteamSetupResult:
    localconfig_path: Path
    steam_config_path: Path
    launch_options_changed: bool
    compat_tool_changed: bool
    localconfig_backup: Path | None
    steam_config_backup: Path | None
    dry_run: bool

    def format_text(self) -> str:
        return "\n".join(
            [
                f"localconfig={self.localconfig_path}",
                f"steam_config={self.steam_config_path}",
                f"dry_run={self.dry_run}",
                f"launch_options_changed={self.launch_options_changed}",
                f"compat_tool_changed={self.compat_tool_changed}",
                f"localconfig_backup={self.localconfig_backup or 'not-written'}",
                f"steam_config_backup={self.steam_config_backup or 'not-written'}",
                f"launch_options={RE9_PRESENT_LAUNCH_OPTIONS}",
                f"compat_tool={RE9_PRESENT_COMPAT_TOOL}",
            ]
        )


def running_steam_processes() -> tuple[str, ...]:
    result = subprocess.run(
        [
            "pgrep",
            "-af",
            "steam|steamwebhelper|wineserver|SteamLaunch|re9|Resident|BIOHAZARD",
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
            "penguin-burner-steam-re9-patched-setup" in line
            or "latency_telemetry.steam_re9_setup" in line
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


def _find_path_with_launch_options(paths: list[Path], app_id: str) -> Path:
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if launch_options_from_localconfig(text, app_id) is not None:
            return path
    raise SteamConfigError(f"Could not find LaunchOptions for Steam app {app_id}.")


def _find_path_with_compat_mapping(paths: list[Path], app_id: str) -> Path:
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if compat_tool_from_config(text, app_id) is not None:
            return path
    raise SteamConfigError(f"Could not find CompatToolMapping for Steam app {app_id}.")


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

    raise SteamConfigError(f"Could not update LaunchOptions for Steam app {app_id}.")


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
                    break
                continue
            if not entered_block or depth != 1:
                continue
            if _quoted_tokens(block_line) != [app_id]:
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

    raise SteamConfigError(f"Could not update CompatToolMapping for Steam app {app_id}.")


def _backup_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.pburn-bak")


def _write_with_backup(path: Path, text: str) -> Path:
    backup = _backup_path(path)
    backup.write_text(
        path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8"
    )
    path.write_text(text, encoding="utf-8")
    return backup


def apply_patched_re9_setup(
    *,
    localconfig_path: Path | None = None,
    steam_config_path: Path | None = None,
    dry_run: bool = False,
    check_running: bool = True,
    wait: bool = False,
    wait_timeout_s: float | None = None,
    poll_interval_s: float = 2.0,
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

    localconfig_path = localconfig_path or _find_path_with_launch_options(
        default_localconfig_paths(), RE9_APP_ID
    )
    steam_config_path = steam_config_path or _find_path_with_compat_mapping(
        default_steam_config_paths(), RE9_APP_ID
    )

    localconfig_text = localconfig_path.read_text(encoding="utf-8", errors="replace")
    steam_config_text = steam_config_path.read_text(encoding="utf-8", errors="replace")

    new_localconfig_text, launch_changed = set_launch_options_in_localconfig(
        localconfig_text,
        app_id=RE9_APP_ID,
        launch_options=RE9_PRESENT_LAUNCH_OPTIONS,
    )
    new_steam_config_text, compat_changed = set_compat_tool_in_config(
        steam_config_text,
        app_id=RE9_APP_ID,
        compat_tool=RE9_PRESENT_COMPAT_TOOL,
    )

    localconfig_backup = None
    steam_config_backup = None
    if not dry_run:
        if launch_changed:
            localconfig_backup = _write_with_backup(localconfig_path, new_localconfig_text)
        if compat_changed:
            steam_config_backup = _write_with_backup(steam_config_path, new_steam_config_text)

        launch_check = check_launch_options(
            app_id=RE9_APP_ID,
            required_tokens=RE9_REQUIRED_TOKENS + RE9_PRESENT_EXTRA_TOKENS,
            config_paths=[localconfig_path],
        )
        compat_check = check_compat_tool(
            app_id=RE9_APP_ID,
            expected_tool=RE9_PRESENT_COMPAT_TOOL,
            config_paths=[steam_config_path],
        )
        if not launch_check.ok or not compat_check.ok:
            raise SteamConfigError("Patched RE9 setup did not verify after writing.")

    return SteamSetupResult(
        localconfig_path=localconfig_path,
        steam_config_path=steam_config_path,
        launch_options_changed=launch_changed,
        compat_tool_changed=compat_changed,
        localconfig_backup=localconfig_backup,
        steam_config_backup=steam_config_backup,
        dry_run=dry_run,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply the RE9 PenguinBurner present-cadence Steam launch configuration."
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
    args = parser.parse_args(argv)

    try:
        result = apply_patched_re9_setup(
            localconfig_path=args.localconfig,
            steam_config_path=args.steam_config,
            dry_run=args.dry_run,
            wait=args.wait,
            wait_timeout_s=args.timeout,
            poll_interval_s=args.poll_interval,
        )
    except SteamConfigError as exc:
        print(f"error={exc}")
        return 1

    print(result.format_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
