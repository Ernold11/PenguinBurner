"""Select and use one native or Flatpak installation of a launcher."""
from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path

from .host_process import host_command_path, run_on_host


@cache
def native_command(name: str) -> str | None:
    """Avoid repeated host portal calls while reading a library; refresh clears it."""
    return host_command_path(name)


@dataclass(frozen=True)
class LauncherInstallation:
    name: str
    app_id: str
    root: Path
    flatpak: bool = False
    home: Path | None = None

    @property
    def app_home(self) -> Path:
        return (self.home or Path.home()) / ".var/app" / self.app_id

    @property
    def wrapper(self) -> str:
        return str(self.app_home / "data/penguin-burner/PENGUIN_BURNER") if self.flatpak else "PENGUIN_BURNER"

    def command(self, *args: str) -> list[str] | None:
        if self.flatpak:
            installed = run_on_host(["flatpak", "info", self.app_id])
            if installed is None or installed.returncode:
                return None
            return ["flatpak", "run", self.app_id, *args]
        executable = native_command(self.name)
        return [executable, *args] if executable else None

    def python_command(self, *args: str) -> list[str]:
        prefix = (["flatpak", "run", "--command=python3", self.app_id]
                  if self.flatpak else ["/usr/bin/python3"])
        return [*prefix, *args]

    def ensure_integration(self) -> None:
        if self.flatpak:
            from .flatpak import ensure_integration

            ensure_integration(self)
        else:
            from common.flatpak_wrappers import ensure_host_integration

            ensure_host_integration()


def select_installation(
    name: str, app_id: str, *, native_roots: tuple[Path, ...],
    flatpak_roots: tuple[Path, ...], markers: tuple[str, ...], home: Path | None = None,
) -> LauncherInstallation:
    """Prefer configured native while installed; never launch a different store.

    Missing executables do not hide saved libraries. With no configured library,
    choose an available native executable, otherwise the Flatpak candidate;
    command() verifies that Flatpak is installed before dispatch.
    """
    native = next((root for root in native_roots if any((root / m).exists() for m in markers)), None)
    sandbox = next((root for root in flatpak_roots if any((root / m).exists() for m in markers)), None)
    use_flatpak = sandbox is not None and (native is None or native_command(name) is None)
    if native is None and sandbox is None:
        use_flatpak = native_command(name) is None
    root = (sandbox or flatpak_roots[0]) if use_flatpak else (native or native_roots[0])
    return LauncherInstallation(name, app_id, root, use_flatpak, home)
