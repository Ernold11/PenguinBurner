#!/usr/bin/env python3

from __future__ import annotations

import os
from pathlib import Path
import pwd
import shutil


def repo_root() -> Path:
    return Path(__file__).resolve().parent


def _effective_home() -> Path:
    sudo_user = os.environ.get("SUDO_USER", "").strip()
    if sudo_user:
        try:
            return Path(pwd.getpwnam(sudo_user).pw_dir)
        except KeyError:
            pass
    return Path.home()


def default_user_config_dir() -> Path:
    return _effective_home() / ".config" / "PenguinBurner"


def default_runtime_config_path() -> Path:
    return default_user_config_dir() / "penguin_burner.toml"


def default_cache_root() -> Path:
    xdg_cache_home = os.environ.get("XDG_CACHE_HOME", "").strip()
    if xdg_cache_home:
        return Path(xdg_cache_home).expanduser()
    return _effective_home() / ".cache"


def default_linux_vf_profiles_dir() -> Path:
    return default_user_config_dir() / "linux-vf-profiles"


def default_afterburner_root() -> Path:
    return default_user_config_dir() / "afterburner-profiles"


def managed_afterburner_root() -> Path:
    return default_afterburner_root().resolve()


def resolve_afterburner_root(afterburner_root=None) -> Path:
    if afterburner_root is not None:
        text = str(afterburner_root).strip()
        if text:
            return Path(text).expanduser()

    override = os.environ.get("PENGUIN_BURNER_AFTERBURNER_ROOT", "").strip()
    if override:
        return Path(override).expanduser()
    return default_afterburner_root()


def validate_afterburner_export_root(afterburner_root) -> list[str]:
    root = resolve_afterburner_root(afterburner_root)
    problems = []
    if not afterburner_global_profile(root).is_file():
        problems.append(f"missing {afterburner_global_profile(root).name}")
    if not afterburner_profiles_dir(root).is_dir():
        problems.append(f"missing {afterburner_profiles_dir(root).name}/")
    return problems


def sync_afterburner_export_tree(source_root, destination_root=None) -> Path:
    source_root = resolve_afterburner_root(source_root).resolve()
    problems = validate_afterburner_export_root(source_root)
    if problems:
        raise FileNotFoundError(
            "Invalid Afterburner export directory: "
            + ", ".join(problems)
            + f" under {source_root}"
        )

    if destination_root is None:
        destination_root = managed_afterburner_root()
    else:
        destination_root = resolve_afterburner_root(destination_root).resolve()

    if source_root == destination_root:
        return destination_root

    destination_root.parent.mkdir(parents=True, exist_ok=True)
    if destination_root.exists():
        shutil.rmtree(destination_root)
    shutil.copytree(source_root, destination_root)
    return destination_root


def afterburner_profiles_dir(afterburner_root=None) -> Path:
    return resolve_afterburner_root(afterburner_root) / "Profiles"


def afterburner_global_profile(afterburner_root=None) -> Path:
    return resolve_afterburner_root(afterburner_root) / "MSIAfterburner.cfg"


def discover_afterburner_device_profiles(afterburner_root=None):
    return sorted(afterburner_profiles_dir(afterburner_root).glob("VEN_*.cfg"))


def default_afterburner_profiles_dir() -> Path:
    return afterburner_profiles_dir()


def default_afterburner_global_profile() -> Path:
    return afterburner_global_profile()


def default_afterburner_device_profile() -> Path:
    matches = discover_afterburner_device_profiles()
    if matches:
        return matches[0]
    return default_afterburner_profiles_dir() / "GPU_PROFILE.cfg"
