"""Refuse Play while a game's Wine prefix runs a program our wrapper did not start.

Store clients -- the EA App, Battle.net, Ubisoft Connect and the like -- keep
running in the prefix once opened, and when asked to start a game they start
it themselves. Opened on their own, outside our wrapper, the game they start
inherits none of our environment: no overlay, no GPU profile, and a session
Stop cannot reach. Opened by Play, they run inside the wrapper and pass it on.

A launcher adapter only says where a game's prefix is. Which programs run in
it is read from the host's /proc, like every other session probe here.
"""

from __future__ import annotations

import json

from overlay.wrapper_tokens import GAME_KEY_ENV

from .host_process import run_on_host
from .wrapped_sessions import host_python

# Wine's own processes run in every prefix and start nothing on their own.
_WINE_INFRASTRUCTURE = frozenset({
    "conhost.exe", "explorer.exe", "plugplay.exe", "rpcss.exe", "rundll32.exe",
    "services.exe", "start.exe", "steam.exe", "svchost.exe", "tabtip.exe",
    "winedevice.exe", "xalia.exe", "wineboot.exe", "winemenubuilder.exe",
})

_CLIENT_NAMES = {
    "eadesktop.exe": "EA App",
    "ealauncher.exe": "EA App",
    "ealaunchhelper.exe": "EA App",
    "origin.exe": "EA App",
    "battle.net.exe": "Battle.net",
    "agent.exe": "Battle.net",
    "upc.exe": "Ubisoft Connect",
    "ubisoftconnect.exe": "Ubisoft Connect",
    "uplay.exe": "Ubisoft Connect",
    "epicgameslauncher.exe": "Epic Games Launcher",
    "galaxyclient.exe": "GOG Galaxy",
    "amazon games.exe": "Amazon Games",
    "wgc.exe": "Wargaming Game Center",
}

# Prints the Windows program names running in argv[1]'s prefix without our
# game key. Stdlib only: in a Flatpak it runs on the host.
_PROBE = r"""
import json, os, sys
from pathlib import Path

prefix = os.path.realpath(sys.argv[1])
found = set()
for proc in Path(sys.argv[3]).iterdir():
    if not proc.name.isdecimal():
        continue
    try:
        if proc.stat().st_uid != os.getuid():
            continue
        env = dict(field.split(b'=', 1) for field in (proc / 'environ').read_bytes().split(b'\0') if b'=' in field)
        value = env.get(b'WINEPREFIX')
        if not value or sys.argv[2].encode() in env:
            continue
        if os.path.realpath(os.fsdecode(value)) != prefix:
            continue
        command = os.fsdecode((proc / 'cmdline').read_bytes().split(b'\0')[0])
    except OSError:
        continue
    name = command.replace('\\', '/').rsplit('/', 1)[-1]
    if name.lower().endswith('.exe'):
        found.add(name)
print(json.dumps(sorted(found)))
"""


def unwrapped_programs(prefix: str, *, proc_root: str = "/proc") -> tuple[str, ...] | None:
    """Windows programs in ``prefix`` our wrapper did not start; None if unknowable."""
    if not prefix:
        return ()
    result = run_on_host(
        [host_python(), "-c", _PROBE, prefix, GAME_KEY_ENV, proc_root], capture=True,
    )
    if result is None or result.returncode != 0:
        return None
    try:
        names = [str(name) for name in json.loads(result.stdout)]
    except (ValueError, TypeError):
        return None
    return tuple(name for name in names if name.lower() not in _WINE_INFRASTRUCTURE)


def client_names(programs: tuple[str, ...]) -> tuple[str, ...]:
    """What to call them: the store client's name, else the program's."""
    return tuple(sorted({_CLIENT_NAMES.get(name.lower(), name) for name in programs}))


def prefix_refusal(prefix: str, game_name: str) -> str:
    """Why Play must not start this game now; empty when it may.

    A probe that fails does not block: refusing every launch because /proc
    could not be read would be worse than one launch without the overlay.
    """
    programs = unwrapped_programs(prefix)
    if not programs:
        return ""
    names = client_names(programs)
    verb = "is" if len(names) == 1 else "are"
    return (
        f"{' and '.join(names)} {verb} already running without PenguinBurner. "
        f"Close {'it' if len(names) == 1 else 'them'}, then press Play so "
        f"{game_name} gets the overlay and GPU profile."
    )
