#!/usr/bin/env python3

from __future__ import annotations

import os
from pathlib import Path
import pwd
import shutil


def repo_root() -> Path:
    return Path(__file__).resolve().parent


def _effective_home() -> Path:
    override_home = os.environ.get("PENGUIN_BURNER_HOME", "").strip()
    if override_home:
        return Path(override_home).expanduser()

    for user_env in ("SUDO_USER", "PENGUIN_BURNER_Q2RTX_USER"):
        user = os.environ.get(user_env, "").strip()
        if not user:
            continue
        try:
            return Path(pwd.getpwnam(user).pw_dir)
        except KeyError:
            pass

    uid = os.environ.get("PENGUIN_BURNER_Q2RTX_UID", "").strip()
    if uid:
        try:
            return Path(pwd.getpwuid(int(uid)).pw_dir)
        except (KeyError, ValueError):
            pass
    return Path.home()


def effective_desktop_user_ids() -> tuple[int, int] | None:
    uid_text = (
        os.environ.get("PENGUIN_BURNER_Q2RTX_UID", "").strip()
        or os.environ.get("SUDO_UID", "").strip()
    )
    gid_text = (
        os.environ.get("PENGUIN_BURNER_Q2RTX_GID", "").strip()
        or os.environ.get("SUDO_GID", "").strip()
    )
    uid = _parse_positive_int(uid_text)
    gid = _parse_positive_int(gid_text)
    user = (
        os.environ.get("PENGUIN_BURNER_Q2RTX_USER", "").strip()
        or os.environ.get("SUDO_USER", "").strip()
    )
    if user and (uid is None or gid is None):
        try:
            entry = pwd.getpwnam(user)
        except KeyError:
            entry = None
        if entry is not None:
            uid = entry.pw_uid if uid is None else uid
            gid = entry.pw_gid if gid is None else gid
    if uid is None or gid is None:
        return None
    return uid, gid


def claim_desktop_user_ownership(
    path: str | Path,
    *,
    recursive: bool = False,
    include_parents: bool = False,
) -> None:
    if os.geteuid() != 0:
        return
    ids = effective_desktop_user_ids()
    if ids is None:
        return
    path = Path(path).expanduser()
    if not _is_under_effective_home(path):
        return
    paths = _ownership_paths(path, recursive=recursive)
    if include_parents:
        paths = [*_ownership_parent_paths(path), *paths]
    for item in paths:
        try:
            os.lchown(item, ids[0], ids[1])
        except (FileNotFoundError, PermissionError, OSError):
            continue


def _parse_positive_int(value: str) -> int | None:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _is_under_effective_home(path: Path) -> bool:
    try:
        home = _effective_home().expanduser().resolve(strict=False)
        resolved = path.expanduser().resolve(strict=False)
        return resolved == home or home in resolved.parents
    except OSError:
        return False


def _ownership_paths(path: Path, *, recursive: bool) -> list[Path]:
    if not recursive or not path.exists() or not path.is_dir():
        return [path]
    paths = [path]
    for root, dirs, files in os.walk(path):
        root_path = Path(root)
        paths.extend(root_path / name for name in dirs)
        paths.extend(root_path / name for name in files)
    return paths


def _ownership_parent_paths(path: Path) -> list[Path]:
    try:
        home = _effective_home().expanduser().resolve(strict=False)
        resolved = path.expanduser().resolve(strict=False)
    except OSError:
        return []
    parents = [parent for parent in resolved.parents if parent != home and home in parent.parents]
    return list(reversed(parents))


def default_user_config_dir() -> Path:
    return _effective_home() / ".config" / "PenguinBurner"


def default_saved_uv_dir() -> Path:
    return _effective_home() / "saved-uv"


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
