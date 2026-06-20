from __future__ import annotations

import os
from pathlib import Path
import pwd
import struct

from .constants import (
    PAK_ENTRY_SIZE,
    PREFERRED_AUTO_DEMO_NAMES,
    Q2RTX_BINARY_CANDIDATES,
)
from .models import StabilityTestError


def _validate_demo_name(demo_name: str) -> str:
    normalized = str(demo_name).strip()
    if not normalized:
        raise StabilityTestError("stability demo name must not be empty")
    if normalized.lower() == "auto":
        return "auto"
    if '"' in normalized or "\n" in normalized or "\r" in normalized:
        raise StabilityTestError(
            "stability demo name must not contain quotes or newlines"
        )
    return normalized


def _effective_q2rtx_home() -> Path | None:
    override_user = os.environ.get("PENGUIN_BURNER_Q2RTX_USER", "").strip()
    if override_user:
        try:
            return Path(pwd.getpwnam(override_user).pw_dir)
        except KeyError:
            pass

    sudo_user = os.environ.get("SUDO_USER", "").strip()
    if sudo_user:
        try:
            return Path(pwd.getpwnam(sudo_user).pw_dir)
        except KeyError:
            pass

    home = os.environ.get("HOME", "").strip()
    if home:
        return Path(home).expanduser()
    return Path.home()


def _effective_q2rtx_xdg_dir(kind: str) -> Path | None:
    env_name = {
        "data": "XDG_DATA_HOME",
        "cache": "XDG_CACHE_HOME",
    }.get(kind)
    if env_name is None:
        return None

    if os.geteuid() != 0:
        value = os.environ.get(env_name, "").strip()
        if value:
            return Path(value).expanduser()

    home = _effective_q2rtx_home()
    if home is None:
        return None
    if kind == "data":
        return home / ".local" / "share"
    if kind == "cache":
        return home / ".cache"
    return None


def _default_q2rtx_homedir() -> Path | None:
    xdg_data_home = _effective_q2rtx_xdg_dir("data")
    if xdg_data_home is not None:
        return xdg_data_home / "quake2rtx"
    return None


def _candidate_demo_dirs(workdir: Path) -> list[Path]:
    candidates = [workdir / "baseq2" / "demos"]
    homedir = _default_q2rtx_homedir()
    if homedir is not None:
        candidates.append(homedir / "baseq2" / "demos")
    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.expanduser()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def _candidate_pak_paths(workdir: Path) -> list[Path]:
    candidates = sorted((workdir / "baseq2").glob("pak*.pak"))
    homedir = _default_q2rtx_homedir()
    if homedir is not None:
        candidates.extend(sorted((homedir / "baseq2").glob("pak*.pak")))
    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.expanduser()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def _list_pak_entries(pak_path: Path) -> list[str]:
    try:
        with pak_path.open("rb") as handle:
            header = handle.read(12)
            if len(header) != 12:
                return []
            magic, directory_offset, directory_length = struct.unpack(
                "<4sII",
                header,
            )
            if magic != b"PACK" or directory_length % PAK_ENTRY_SIZE != 0:
                return []
            handle.seek(directory_offset)
            entries: list[str] = []
            for _ in range(directory_length // PAK_ENTRY_SIZE):
                name_bytes = handle.read(56)
                data_bytes = handle.read(8)
                if len(name_bytes) != 56 or len(data_bytes) != 8:
                    return entries
                name = name_bytes.split(b"\x00", 1)[0].decode(
                    "latin-1",
                    errors="replace",
                )
                if name:
                    entries.append(name)
            return entries
    except OSError:
        return []


def _discover_demo_candidates(workdir: Path) -> dict[str, Path | None]:
    discovered: dict[str, Path | None] = {}
    for demo_dir in _candidate_demo_dirs(workdir):
        if not demo_dir.is_dir():
            continue
        for path in sorted(demo_dir.glob("*.dm2")):
            discovered.setdefault(path.stem.lower(), path)

    for pak_path in _candidate_pak_paths(workdir):
        for entry in _list_pak_entries(pak_path):
            if not entry.startswith("demos/") or not entry.endswith(".dm2"):
                continue
            demo_name = Path(entry).stem.lower()
            discovered.setdefault(demo_name, None)
    return discovered


def resolve_workload(
    requested_demo_name: str,
    *,
    workdir: Path,
) -> tuple[str, Path | None]:
    normalized = _validate_demo_name(requested_demo_name)
    normalized_lower = normalized.lower()

    discovered = _discover_demo_candidates(workdir)

    if normalized_lower != "auto":
        demo_path = discovered.get(normalized_lower)
        return normalized, demo_path

    if discovered:
        for name in PREFERRED_AUTO_DEMO_NAMES:
            chosen = discovered.get(name.lower())
            if chosen is not None or name.lower() in discovered:
                return name, chosen
        chosen_name = sorted(discovered)[0]
        return chosen_name, discovered[chosen_name]

    raise StabilityTestError(
        "no .dm2 demo assets were found under the detected Q2RTX data directories"
    )


def _default_q2rtx_roots() -> list[Path]:
    home = _effective_q2rtx_home() or Path.home()
    roots: list[Path] = []

    xdg_data_home = _effective_q2rtx_xdg_dir("data")
    if xdg_data_home is not None:
        penguinburner_root = xdg_data_home / "PenguinBurner" / "q2rtx"
    else:
        penguinburner_root = home / ".local" / "share" / "PenguinBurner" / "q2rtx"

    if penguinburner_root.exists():
        roots.append(penguinburner_root)
        versioned_roots = sorted(
            (path for path in penguinburner_root.iterdir() if path.is_dir()),
            reverse=True,
        )
        roots.extend(versioned_roots)

    return roots


def resolve_q2rtx_executable(*, root: Path | None = None) -> tuple[Path, Path]:
    roots = [root.expanduser().resolve()] if root is not None else _default_q2rtx_roots()
    for candidate_root in roots:
        root = candidate_root.expanduser().resolve()
        if root is None:
            continue
        if not root.exists():
            if candidate_root == roots[0] and len(roots) == 1:
                raise StabilityTestError(f"Managed Q2RTX directory not found: {root}")
            continue
        for relative in Q2RTX_BINARY_CANDIDATES:
            candidate = root / relative
            if candidate.is_file():
                return candidate, resolve_q2rtx_workdir(candidate, root=root)

    raise StabilityTestError(
        "Managed Q2RTX is not installed. Run `python -m stability.q2rtx "
        "--install-q2rtx` or start an Auto-UV/stability run with auto-install enabled."
    )


def resolve_q2rtx_workdir(
    executable_path: Path,
    *,
    root: Path,
) -> Path:
    candidates = [
        root.expanduser().resolve(),
        executable_path.parent,
        executable_path.parent.parent,
        executable_path.parent.parent.parent,
    ]
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if (candidate / "baseq2").exists():
            return candidate
    return root.expanduser().resolve()
