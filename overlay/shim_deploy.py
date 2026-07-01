"""Deploy the PenguinBurner NVAPI latency shim into a Proton prefix.

The shim is a drop-in proxy ``nvapi64.dll`` (see ``native/nvapi_shim/``). When
the in-game latency flag is on, the launcher fronts the prefix's system32
``nvapi64.dll`` with it: the real dxvk-nvapi is copied aside to ``nvapi64-pb.dll``
and our shim takes the ``nvapi64.dll`` name. The shim forwards every call to that
sidecar and taps the Reflex latency markers, emitting them to stderr in the
format ``telemetry/nvapi_marker_bridge.py`` parses -- which the launcher already
routes to the marker FIFO. That replaces enabling dxvk-nvapi trace/marker-log:
same marker stream, no fork, and it works under frame generation (the tap is
above vkd3d's owner-gate).

Why system32 and not the game directory: it is fully generic. Every process that
loads ``nvapi64.dll`` -- bootstrapper launchers, UE shipping exes, Streamline's
``sl.interposer`` (which resolves nvapi64 from system32) -- picks up the shim
with zero per-game or per-engine path logic. Re-applied every launch, so a Proton
prefix re-sync (which restores the stock nvapi64.dll) simply self-heals on the
next start.

Falling back is always safe: when this returns ``None`` the launcher keeps the
existing dxvk-nvapi marker-log / trace path.
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import select
import shutil
import struct
import subprocess
import sys
import time

# Recognises our own DLL; the banner string is compiled into the shim (see
# native/nvapi_shim/src/nvapi_shim.cpp).
SHIM_NEEDLE = b"[pb-nvapi-shim]"
SHIM_DLL_NAME = "nvapi64.dll"
# The real dxvk-nvapi is parked under this name; the shim forwards to it.
REAL_SIDECAR_NAME = "nvapi64-pb.dll"

# Override the directory (or direct file path) the shim artifact is loaded from.
NVAPI_SHIM_DIR_ENV = "PENGUIN_BURNER_NVAPI_SHIM_DIR"
# Force the shim off even when the in-game latency flag is on (falls back to the
# dxvk-nvapi marker-log / trace path).
NVAPI_SHIM_DISABLE_ENV = "PENGUIN_BURNER_NVAPI_SHIM_DISABLE"
# Optional hard cap (seconds) on how long the re-front watcher runs. Unset, the
# watcher runs for the whole Proton session (see watch_and_refront).
NVAPI_SHIM_WATCH_SECONDS_ENV = "PENGUIN_BURNER_NVAPI_SHIM_WATCH_SECONDS"

# Proton's per-launch prefix setup unconditionally try_copy()s its bundled
# dxvk-nvapi over system32\nvapi64.dll (os.remove + copyfile, no content check;
# proton script, the `if use_nvapi:` block of setup_prefix). That runs *after*
# the wrapper's pre-exec deploy but *before* the game loads nvapi64.dll, so a
# single deploy is always clobbered. The watcher re-fronts the shim when that
# happens: inotify on system32 reacts to each finished rewrite of nvapi64.dll
# (falling back to this poll interval if inotify is unavailable), and it stays
# armed for the whole Proton session -- a fixed window would miss slow first
# launches (prefix creation / anticheat installs can outlast any constant) and
# mid-session re-copies (e.g. a compat-config change re-running prefix setup).
_WATCH_POLL_SECONDS = 0.25
# How often the watcher wakes from the inotify wait to check whether the Proton
# session (its parent process) is still alive.
_SESSION_POLL_SECONDS = 2.0

_OVERLAY_ROOT = Path(__file__).resolve().parent
# Packaged location (populated by the build) and the source-checkout build dir.
_PACKAGED_SHIM_DLL = _OVERLAY_ROOT / "nvapi_shim" / SHIM_DLL_NAME
_SOURCE_SHIM_DLL = _OVERLAY_ROOT / "native" / "nvapi_shim" / "build" / SHIM_DLL_NAME

_TRUTHY = {"1", "true", "yes", "on"}


def nvapi_shim_artifact(env: dict[str, str] | None = None) -> Path | None:
    """Return the path to the built shim nvapi64.dll, or None if unavailable."""
    source = os.environ if env is None else env
    candidates: list[Path] = []
    explicit = str(source.get(NVAPI_SHIM_DIR_ENV) or "").strip()
    if explicit:
        base = Path(explicit).expanduser()
        candidates.append(base / SHIM_DLL_NAME)
        candidates.append(base)  # allow pointing straight at the file
    candidates.append(_PACKAGED_SHIM_DLL)
    candidates.append(_SOURCE_SHIM_DLL)
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def prefix_system32(env: dict[str, str]) -> Path | None:
    """The running prefix's system32 directory, if it exists."""
    data_path = str(env.get("STEAM_COMPAT_DATA_PATH") or "").strip()
    if not data_path:
        return None
    system32 = (
        Path(data_path).expanduser() / "pfx" / "drive_c" / "windows" / "system32"
    )
    try:
        return system32 if system32.is_dir() else None
    except OSError:
        return None


def _file_contains(path: Path, needle: bytes) -> bool:
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                if needle in chunk:
                    return True
    except OSError:
        return False
    return False


def _files_identical(left: Path, right: Path) -> bool:
    try:
        if left.stat().st_size != right.stat().st_size:
            return False
        with left.open("rb") as a, right.open("rb") as b:
            while True:
                chunk_a = a.read(1024 * 1024)
                chunk_b = b.read(1024 * 1024)
                if chunk_a != chunk_b:
                    return False
                if not chunk_a:
                    return True
    except OSError:
        return False


def _atomic_copy(src: Path, dst: Path) -> None:
    tmp = dst.with_name(dst.name + ".pb-tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def deploy_nvapi_shim(env: dict[str, str]) -> Path | None:
    """Front the prefix's system32 nvapi64.dll with the shim. Return its path, or None.

    Idempotent and self-healing:
    - stock nvapi64.dll present -> park it as nvapi64-pb.dll, install the shim;
    - shim already installed -> refresh it if the build changed;
    - prefix re-sync restored the stock DLL -> re-park and re-install next launch.

    Returns None (launcher falls back to dxvk-nvapi marker-log / trace) when the
    shim is disabled or unbuilt, there is no prefix, or system32 is not writable.
    """
    if str(env.get(NVAPI_SHIM_DISABLE_ENV) or "").strip().lower() in _TRUTHY:
        return None

    artifact = nvapi_shim_artifact(env)
    if artifact is None:
        return None

    system32 = prefix_system32(env)
    if system32 is None:
        return None

    nvapi = system32 / SHIM_DLL_NAME
    sidecar = system32 / REAL_SIDECAR_NAME

    try:
        if not nvapi.is_file():
            return None  # nothing to forward to / prefix not ready yet

        if _file_contains(nvapi, SHIM_NEEDLE):
            # Already our shim. Refresh it if the build changed; the sidecar (real
            # dxvk-nvapi) is left as-is.
            if not sidecar.is_file():
                _log(f"nvapi shim: {sidecar.name} missing -- shim has no forward target")
                return None
            if not _files_identical(nvapi, artifact):
                _atomic_copy(artifact, nvapi)
                _log(f"nvapi shim: updated {nvapi}")
            return nvapi

        # nvapi64.dll is the stock dxvk-nvapi (fresh install or post re-sync):
        # park it under the sidecar name, then front it with the shim.
        _atomic_copy(nvapi, sidecar)
        _atomic_copy(artifact, nvapi)
        _log(f"nvapi shim: installed {nvapi} (real -> {sidecar.name})")
        return nvapi
    except OSError as error:
        _log(f"nvapi shim: deploy failed in {system32}: {error}")
        return None


def watch_seconds(env: dict[str, str]) -> float | None:
    """Optional hard cap on the watch duration; None = whole Proton session."""
    raw = str(env.get(NVAPI_SHIM_WATCH_SECONDS_ENV) or "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return None


# inotify(7) constants (linux/inotify.h); PenguinBurner is Linux-only.
_IN_CLOSE_WRITE = 0x00000008
_IN_MOVED_TO = 0x00000080
_IN_Q_OVERFLOW = 0x00004000
_IN_IGNORED = 0x00008000
_INOTIFY_EVENT_HEADER = struct.Struct("iIII")  # wd, mask, cookie, len


def _parse_inotify_events(buf: bytes) -> list[tuple[int, str]]:
    """Decode an inotify read() buffer into (mask, name) pairs."""
    events: list[tuple[int, str]] = []
    offset = 0
    header = _INOTIFY_EVENT_HEADER
    while offset + header.size <= len(buf):
        fields = header.unpack_from(buf, offset)
        mask, name_len = fields[1], fields[3]
        offset += header.size
        raw_name = buf[offset : offset + name_len].split(b"\0", 1)[0]
        offset += name_len
        events.append((mask, os.fsdecode(raw_name)))
    return events


class _Nvapi64Notifier:
    """inotify watch on system32, reporting finished rewrites of nvapi64.dll.

    Only ``IN_CLOSE_WRITE`` / ``IN_MOVED_TO`` are watched: reacting to creation
    would race Proton's in-progress copy and could park a half-written DLL as
    the shim's forward target. A queue overflow or a dropped watch (prefix
    recreated) also reports "changed" so the caller re-checks the file.
    """

    _MASK = _IN_CLOSE_WRITE | _IN_MOVED_TO

    def __init__(self, directory: Path) -> None:
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._dir = os.fsencode(str(directory))
        fd = self._libc.inotify_init1(os.O_CLOEXEC)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self.fd = fd
        try:
            self._add_watch()
        except OSError:
            self.close()
            raise

    def _add_watch(self) -> None:
        if self._libc.inotify_add_watch(self.fd, self._dir, self._MASK) < 0:
            raise OSError(ctypes.get_errno(), "inotify_add_watch failed")

    def drain(self) -> bool:
        """Consume queued events; True when nvapi64.dll may have been rewritten."""
        changed = False
        for mask, name in _parse_inotify_events(os.read(self.fd, 4096)):
            if mask & _IN_Q_OVERFLOW:
                changed = True  # events lost; caller must re-check
            if mask & _IN_IGNORED:
                changed = True
                self._add_watch()  # dir was replaced; re-arm (raises if gone)
            if name == SHIM_DLL_NAME:
                changed = True
        return changed

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


def _open_notifier(system32: Path | None) -> "_Nvapi64Notifier | None":
    if system32 is None:
        return None
    try:
        return _Nvapi64Notifier(system32)
    except OSError as error:
        _log(f"nvapi shim: inotify unavailable ({error}); watcher falls back to polling")
        return None


def _open_session_fd(session_pid: int) -> int | None:
    """A pidfd for the Proton session, so its exit wakes the watcher's select.

    None when pidfds are unavailable (the watcher then polls the pid's
    liveness) or the session is already gone (the first liveness check ends
    the watch).
    """
    pidfd_open = getattr(os, "pidfd_open", None)
    if pidfd_open is None:
        return None
    try:
        return pidfd_open(session_pid)
    except OSError:
        return None


def _session_alive(session_pid: int, session_fd: int | None) -> bool:
    if session_fd is not None:
        return True  # the select on the pidfd reports the exit instead
    try:
        os.kill(session_pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def watch_and_refront(
    env: dict[str, str],
    *,
    duration_s: float | None = None,
    poll_s: float = _WATCH_POLL_SECONDS,
    session_pid: int | None = None,
) -> None:
    """Keep the shim fronted across Proton's per-launch nvapi64.dll copies.

    Deploys once, then re-runs ``deploy_nvapi_shim`` whenever inotify reports
    system32's ``nvapi64.dll`` was rewritten (Proton's prefix setup does that
    unconditionally every launch), polling instead if inotify is unavailable.

    Runs until the Proton session exits: ``session_pid`` is the wrapper's pid,
    which ``os.execvpe`` turns *into* the Proton session, watched via a pidfd
    (with a liveness poll as fallback). Defaults to our parent when not given
    (direct invocation). ``duration_s`` (or the env override) caps the run
    instead when set. Meant to run detached (see ``spawn_refront_watcher``).
    """
    if duration_s is None:
        duration_s = watch_seconds(env)
    deadline = (
        None if duration_s is None else time.monotonic() + max(0.0, float(duration_s))
    )
    if session_pid is None:
        session_pid = os.getppid()
    session_fd = _open_session_fd(session_pid)
    notifier = _open_notifier(prefix_system32(env))
    try:
        deploy_nvapi_shim(env)
        while True:
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                return
            if not _session_alive(session_pid, session_fd):
                return  # Proton session exited; nothing left to guard
            wait_s = _SESSION_POLL_SECONDS if notifier is not None else max(0.0, poll_s)
            if deadline is not None:
                wait_s = min(wait_s, max(0.0, deadline - now))
            fds = [fd for fd in ((notifier.fd if notifier else None), session_fd) if fd is not None]
            ready: list[int] = []
            if fds:
                ready = select.select(fds, [], [], wait_s)[0]
            else:
                time.sleep(wait_s)
            if session_fd is not None and session_fd in ready:
                return  # Proton session exited
            if notifier is None:
                deploy_nvapi_shim(env)  # polling fallback: re-front each cadence
            elif notifier.fd in ready:
                try:
                    if notifier.drain():
                        deploy_nvapi_shim(env)
                except OSError:
                    # Watch lost (e.g. prefix recreated): degrade to polling.
                    notifier.close()
                    notifier = None
    finally:
        if notifier is not None:
            notifier.close()
        if session_fd is not None:
            try:
                os.close(session_fd)
            except OSError:
                pass


def spawn_refront_watcher(env: dict[str, str]) -> "subprocess.Popen | None":
    """Launch ``watch_and_refront`` as a detached process that outlives exec().

    The wrapper ``os.execvpe``s into Proton right after configuring the env, so
    the re-fronting must live in a separate, session-detached process. Our own
    pid is passed along: exec keeps it, so it *is* the Proton session's pid and
    the watcher ends when that session does. Returns the Popen, or None when
    there is nothing to front or the spawn fails.
    """
    if str(env.get(NVAPI_SHIM_DISABLE_ENV) or "").strip().lower() in _TRUTHY:
        return None
    if nvapi_shim_artifact(env) is None or prefix_system32(env) is None:
        return None
    try:
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "overlay.shim_deploy",
                "--watch",
                f"--session-pid={os.getpid()}",
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=sys.stderr,
            start_new_session=True,
            close_fds=True,
        )
    except (OSError, ValueError) as error:
        _log(f"nvapi shim: re-front watcher failed to start: {error}")
        return None


def _log(message: str) -> None:
    # Wrapper stderr here is the inherited console / proton log (the marker FIFO
    # redirect happens later), so this is a human diagnostic, not marker data.
    print(message, file=sys.stderr)


def _main(argv: list[str]) -> int:
    env = dict(os.environ)
    session_pid: int | None = None
    for arg in argv:
        if arg.startswith("--session-pid="):
            try:
                session_pid = int(arg.split("=", 1)[1])
            except ValueError:
                pass
    if "--watch" in argv:
        watch_and_refront(env, session_pid=session_pid)
    else:
        deploy_nvapi_shim(env)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
