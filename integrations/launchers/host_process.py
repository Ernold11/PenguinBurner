"""Asking the host about programs and processes, from inside a Flatpak or not.

Every launcher integration needs the same three answers -- is this program
installed, what is running, stop that pid -- and inside a Flatpak all three
have to leave the sandbox. It has its own PATH and its own PID namespace, so
an in-sandbox answer describes nothing the user's games actually run in: the
binary would be missing, and a pid would be nothing or, worse, some unrelated
sandbox process.

Steam and Lutris each carried their own copy of this bridge, and every
launcher added after them would have carried another. One copy means a
launcher added later inherits the sandbox behaviour instead of rediscovering
it.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from pathlib import Path

FLATPAK_INFO_PATH = Path("/.flatpak-info")
HOST_PGREP = "/usr/bin/pgrep"
HOST_KILL = "/usr/bin/kill"
HOST_SHELL = "/usr/bin/sh"
#: flatpak-spawn otherwise mirrors the sandbox cwd, and app-only paths such as
#: /app do not exist on the host -- the portal rejects the command before the
#: launcher ever sees it. A neutral host directory makes every caller
#: independent of how the Flatpak itself was started.
HOST_WORKING_DIRECTORY = "/tmp"

DEFAULT_TIMEOUT_S = 3.0


def running_in_flatpak() -> bool:
    return bool(os.environ.get("FLATPAK_ID", "").strip()) or FLATPAK_INFO_PATH.is_file()


def host_command(command: list[str]) -> list[str] | None:
    """``command`` as it must be spelled to run on the host.

    Outside a Flatpak that is the command itself. Inside one it goes through
    flatpak-spawn, and None means the portal is unreachable -- which is not
    the same as the command failing, so callers report it as "unknown".
    """
    if not running_in_flatpak():
        return list(command)
    flatpak_spawn = shutil.which("flatpak-spawn")
    if not flatpak_spawn:
        return None
    return [
        flatpak_spawn,
        "--host",
        f"--directory={HOST_WORKING_DIRECTORY}",
        *command,
    ]


def run_on_host(
    command: list[str],
    *,
    capture: bool = False,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> subprocess.CompletedProcess | None:
    """Run to completion on the host. None means it could not be run at all."""
    resolved = host_command(command)
    if not resolved:
        return None
    try:
        if capture:
            return subprocess.run(
                resolved,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        return subprocess.run(
            resolved,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def start_on_host(command: list[str]) -> bool:
    """Start a detached command on the host and stop caring about it."""
    resolved = host_command(command)
    if not resolved:
        return False
    try:
        subprocess.Popen(
            resolved,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False
    return True


def host_has_command(name: str) -> bool:
    """Whether ``name`` is on the host's PATH."""
    if not running_in_flatpak():
        return shutil.which(name) is not None
    result = run_on_host([HOST_SHELL, "-c", f"command -v {name}"])
    return result is not None and result.returncode == 0


def host_pgrep(pattern: str) -> list[tuple[int, str]] | None:
    """``pgrep -af`` on the host: (pid, command line) for every match.

    None means the check itself failed -- not that nothing matched -- so a
    caller can hold what it knows instead of reading a stalled probe as every
    game having exited.
    """
    result = run_on_host([HOST_PGREP, "-af", pattern], capture=True)
    if result is None:
        return None
    # 1 = ran fine and matched nothing; anything but 0/1 means the answer is
    # not trustworthy.
    if result.returncode not in (0, 1):
        return None
    matches: list[tuple[int, str]] = []
    for line in (result.stdout or "").splitlines():
        pid_text, _, rest = line.partition(" ")
        if pid_text.isdigit():
            matches.append((int(pid_text), rest))
    return matches


def host_terminate(pid: int) -> bool:
    """One SIGTERM, delivered where the pid actually means something."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if not running_in_flatpak():
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return False
        return True
    result = run_on_host([HOST_KILL, "-TERM", str(pid)])
    return result is not None and result.returncode == 0
