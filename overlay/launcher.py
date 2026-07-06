from __future__ import annotations

import errno
import os
from pathlib import Path
import re
import stat
import sys

from .config import OVERLAY_CONFIG_ENV, default_overlay_config_path
from .native_layer import LATENCY_LAYER_NAME
from .native_layer import native_layer_dirs
from .shim_deploy import deploy_nvapi_shim, spawn_refront_watcher
from .telemetry.nvapi_marker_bridge import spawn_detached_drainer
from .state import (
    OVERLAY_ENABLE_ENV_ALIAS,
    OVERLAY_ENABLE_ENV,
    OVERLAY_STATE_ENV,
    OVERLAY_TEXT_ENV,
    overlay_state_path,
    overlay_text_path,
)
from .telemetry.steam_launch_check import PENGUIN_BURNER_WRAPPER

MASTER_ENABLE_ENV = "PENGUIN_BURNER"
LATENCY_ENABLE_ENV = "PENGUIN_BURNER_LATENCY_LAYER"
LATENCY_SOCKET_ENV = "PENGUIN_BURNER_LATENCY_SOCKET"
# User-facing toggle for extra in-game Reflex marker telemetry. Native Vulkan
# marker latency stays enabled by the wrapper; this toggle asks dxvk-nvapi to
# emit marker records too. New dxvk-nvapi builds use a marker-only log flag;
# full trace remains an explicit/manual fallback.
INGAME_LATENCY_ENV = "PENGUIN_BURNER_INGAME_LATENCY"
INGAME_LATENCY_ENV_ALIAS = "PB_INGAME_LATENCY"
# Display (present->scanout) latency is folded into the single in-game latency
# opt-in: when latency is on, the wrapper also enables the present-wait tail and
# the present-id injection that makes it work on vkd3d titles. These stay env
# vars (not user-facing tokens) so one launch-line flag covers the whole stack.
DISPLAY_LATENCY_ENV = "PENGUIN_BURNER_LATENCY_DISPLAY"
INJECT_PRESENT_ID_ENV = "PENGUIN_BURNER_LATENCY_INJECT_PRESENT_ID"
DXVK_NVAPI_ENABLE_ENV = "DXVK_NVAPI_VKREFLEX"
DXVK_NVAPI_MARKER_LOG_ENV = "DXVK_NVAPI_LATENCY_MARKER_LOG"
DXVK_NVAPI_TRACE_ENV = "DXVK_NVAPI_LOG_LEVEL"
VK_LAYER_PATH_ENV = "VK_ADD_IMPLICIT_LAYER_PATH"
VK_LAYER_ENABLE_ENV = "VK_LOADER_LAYERS_ENABLE"

_TRUTHY = {"1", "true", "yes", "on"}
_FALSEY = {"0", "false", "no", "off"}


def ingame_latency_enabled(env: dict[str, str]) -> bool:
    for key in (INGAME_LATENCY_ENV, INGAME_LATENCY_ENV_ALIAS):
        if str(env.get(key) or "").strip().lower() in _FALSEY:
            return False
    for key in (INGAME_LATENCY_ENV, INGAME_LATENCY_ENV_ALIAS):
        if str(env.get(key) or "").strip().lower() in _TRUTHY:
            return True
    # No explicit setting: default the latency meter ON whenever the overlay is
    # enabled, so turning on the overlay just gives you latency too. Opt out with
    # PENGUIN_BURNER_INGAME_LATENCY=0 (or PB_INGAME_LATENCY=0).
    return _overlay_enabled(env)


def _overlay_enabled(env: dict[str, str]) -> bool:
    for key in (OVERLAY_ENABLE_ENV, OVERLAY_ENABLE_ENV_ALIAS):
        value = str(env.get(key) or "").strip().lower()
        if value in _FALSEY:
            return False
        if value:  # "1", "auto", etc.
            return True
    return False

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DXVK_NVAPI_LAYER_DIR = _REPO_ROOT / "third_party" / "dxvk-nvapi" / "build.layer"
_DXVK_NVAPI_LAYER_NAME = "VK_LAYER_DXVK_NVAPI_reflex"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(
            f"Usage: {PENGUIN_BURNER_WRAPPER} %command%\n"
            f"Steam launch option example: {PENGUIN_BURNER_WRAPPER} %command%",
            file=sys.stderr,
        )
        return 2

    env = dict(os.environ)
    shim_active = configure_penguin_burner_environment(env, command_args=args)
    _remove_mangohud_environment(env)
    _prepare_overlay_paths(env)
    if shim_active:
        # Proton clobbers our pre-exec shim during prefix setup; this detached
        # watcher re-fronts it across that window and survives the exec below.
        # Spawn it before _route_trace_to_fifo so the watcher inherits the
        # console stderr -- not the marker FIFO that fd 2 is about to become,
        # which would feed its diagnostics into the bridge as bogus markers.
        spawn_refront_watcher(env)
    if ingame_latency_enabled(env):
        # The FIFO must always have a drainer for as long as it can have
        # writers, or the 64 KB pipe fills and the shim's marker write (and any
        # stderr write) blocks the game. The drainer's lifetime is tied to this
        # pid (the exec below turns it into the Proton session) plus the FIFO's
        # writer count -- NOT to the app, which may be closed the whole time.
        # Same stderr caveat as the watcher: spawn before the redirect.
        _sweep_stale_marker_fifos(env)
        if _create_marker_fifo(env):
            spawn_detached_drainer(
                env, trace_fifo_path(env), session_pid=os.getpid()
            )
        _route_trace_to_fifo(env)
    os.execvpe(args[0], args, env)
    return 127


SHIM_OUTPUT_ENV = "PENGUIN_BURNER_SHIM_OUTPUT"
# The per-launch marker FIFO path, pinned into the env so the wrapper, the shim
# (via its Z:\ wine path) and the detached drainer all agree on one file.
MARKER_FIFO_ENV = "PENGUIN_BURNER_MARKER_FIFO"


def trace_fifo_path(env: dict[str, str]) -> Path:
    """The marker FIFO for this launch.

    One FIFO *per game session* (named by the wrapper's pid, which exec turns
    into the Proton session's): each launch gets its own drainer, so two
    concurrently wrapped games never share a pipe -- a shared FIFO with one
    reader per wrapper would have the readers stealing each other's lines.
    """
    explicit = str(env.get(MARKER_FIFO_ENV) or "").strip()
    if explicit:
        return Path(explicit)
    return _home_latency_socket_path(env).with_name(
        f"nvapi-trace.{os.getpid()}.fifo"
    )


def _fifo_wine_path(env: dict[str, str]) -> str:
    # Wine maps the unix filesystem root to Z:. The shim runs inside the prefix,
    # so the marker FIFO (created by the launcher on the host) is reachable at
    # its Z:\ path; opening it by path sidesteps the broken inherited fd 2.
    return "Z:" + str(trace_fifo_path(env)).replace("/", "\\")


def _create_marker_fifo(env: dict[str, str]) -> bool:
    """Create this launch's marker FIFO. False when the filesystem refuses."""
    fifo = trace_fifo_path(env)
    try:
        fifo.parent.mkdir(parents=True, exist_ok=True)
        if not fifo.exists():
            os.mkfifo(fifo, 0o600)
        return True
    except OSError:
        return False


def _sweep_stale_marker_fifos(env: dict[str, str]) -> None:
    """Unlink leftover per-launch marker FIFOs that no drainer reads anymore.

    The drainer unlinks its own FIFO on exit; this catches the crash/SIGKILL
    leftovers. A non-blocking write-only open of a FIFO fails with ENXIO
    exactly when it has no reader -- a live drainer means skip it.
    """
    current = trace_fifo_path(env)
    try:
        siblings = list(current.parent.glob("nvapi-trace*.fifo"))
    except OSError:
        return
    for path in siblings:
        if path == current:
            continue
        try:
            if not stat.S_ISFIFO(path.stat().st_mode):
                continue
            fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as error:
            if error.errno == errno.ENXIO:
                try:
                    path.unlink()
                except OSError:
                    pass
            continue
        os.close(fd)  # a reader exists: another launch's drainer is live


def _route_trace_to_fifo(env: dict[str, str]) -> None:
    """Send dxvk-nvapi marker output into an in-memory FIFO.

    dxvk-nvapi marker-only logging and the older full-trace fallback both write
    to wine's debug output, i.e. the process stderr. We point stderr at a host
    FIFO that the marker bridge drains, so the marker stream lives in the
    kernel pipe buffer (RAM) and never touches disk. Redirecting the inherited
    fd (not a path) also avoids any wine path translation.
    """
    fifo = trace_fifo_path(env)
    try:
        fifo.parent.mkdir(parents=True, exist_ok=True)
        if not fifo.exists():
            os.mkfifo(fifo, 0o600)
        # O_RDWR so the open never blocks even if the bridge has not opened the
        # read end yet (a FIFO opened write-only would block until a reader).
        fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        os.set_blocking(fd, True)
        os.dup2(fd, 2)
        if fd != 2:
            os.close(fd)
    except OSError:
        # Fall back silently to the inherited stderr; the overlay still shows
        # FPS/clocks, just no in-game latency.
        return


def configure_penguin_burner_environment(
    env: dict[str, str],
    *,
    command_args: list[str] | tuple[str, ...] | None = None,
) -> bool:
    """Populate the launch env. Return True when the NVAPI shim is the marker
    source (so the caller arms the re-front watcher)."""
    overlay_config_path = default_overlay_config_path(env)
    env.setdefault(OVERLAY_CONFIG_ENV, str(overlay_config_path))
    env.setdefault(MASTER_ENABLE_ENV, "1")
    env.setdefault(LATENCY_ENABLE_ENV, "1")
    _apply_overlay_enable_alias(env)
    env.setdefault(OVERLAY_ENABLE_ENV, "auto")
    env.setdefault(DXVK_NVAPI_ENABLE_ENV, "1")
    env.setdefault("PROTON_ENABLE_NVAPI", "1")
    env.setdefault("PROTON_HIDE_NVIDIA_GPU", "0")
    shim_active = False
    if ingame_latency_enabled(env):
        # Pin this launch's marker FIFO before anything derives from it (the
        # shim's SHIM_OUTPUT wine path, the drainer's --log, the stderr route).
        env.setdefault(MARKER_FIFO_ENV, str(trace_fifo_path(env)))
        shim_active = _configure_dxvk_nvapi_marker_output(
            env, command_args=command_args
        )
        # Fold the present->scanout display tail into the same opt-in. Both are
        # read-only/gated in the layer and inert where unsupported.
        env.setdefault(DISPLAY_LATENCY_ENV, "1")
        env.setdefault(INJECT_PRESENT_ID_ENV, "1")
    env.setdefault(LATENCY_SOCKET_ENV, str(_home_latency_socket_path(env)))
    env.setdefault(OVERLAY_STATE_ENV, str(overlay_state_path(env)))
    env.setdefault(OVERLAY_TEXT_ENV, str(overlay_text_path(env)))
    _prepend_layer_paths(env)
    _prepend_enabled_layers(env)
    return shim_active


def _configure_dxvk_nvapi_marker_output(
    env: dict[str, str],
    *,
    command_args: list[str] | tuple[str, ...] | None = None,
) -> bool:
    _ = command_args
    # Debug escape: if the user explicitly forces dxvk-nvapi's own trace log
    # (DXVK_NVAPI_LOG_LEVEL=trace), skip the shim and let it log. That path is
    # heavy (every nvapi call) -- diagnostics only; the shim is the normal source.
    if str(env.get(DXVK_NVAPI_TRACE_ENV) or "").strip().lower() == "trace":
        return False

    # The drop-in NVAPI shim taps the Reflex markers above vkd3d's owner-gate (so
    # it works under frame generation) and writes them to the marker FIFO the
    # bridge drains -- no trace, no marker-log, no dxvk-nvapi fork. When there is
    # no prefix to front, in-game latency falls back to the Vulkan layer's own
    # vkSetLatencyMarkerNV tap (which covers the single-swapchain / non-FG case).
    if deploy_nvapi_shim(env):
        # The shim's default write to fd 2 silently fails in a GUI game process
        # (msvcrt's std fd 2 isn't a writable handle for our loaded DLL: _write(2)
        # returns EBADF with no syscall). Hand it the FIFO's wine path so it
        # _open()s a fresh, valid fd. A user-set SHIM_OUTPUT (debug file) wins.
        env.setdefault(SHIM_OUTPUT_ENV, _fifo_wine_path(env))
        return True

    return False


def _apply_overlay_enable_alias(env: dict[str, str]) -> None:
    if OVERLAY_ENABLE_ENV in env:
        return
    value = str(env.get(OVERLAY_ENABLE_ENV_ALIAS) or "").strip()
    if value:
        env[OVERLAY_ENABLE_ENV] = value


def _remove_mangohud_environment(env: dict[str, str]) -> None:
    for key in tuple(env):
        upper = key.upper()
        if upper.startswith("MANGOHUD") or upper.startswith("MANGOAPP"):
            env.pop(key, None)

    preload = str(env.get("LD_PRELOAD") or "").strip()
    if not preload:
        return
    entries = [
        entry
        for entry in re.split(r"[:\s]+", preload)
        if entry and "mangohud" not in entry.lower()
    ]
    if entries:
        env["LD_PRELOAD"] = ":".join(entries)
    else:
        env.pop("LD_PRELOAD", None)


def _home_latency_socket_path(env: dict[str, str]) -> Path:
    home = str(env.get("HOME") or "").strip()
    if home and home != "/root":
        return Path(home).expanduser() / ".cache" / "penguin-burner" / "latency.sock"
    state_path = overlay_state_path(env)
    return state_path.with_name("latency.sock")


def _prepend_layer_paths(env: dict[str, str]) -> None:
    layer_paths = [
        *(str(path) for path in native_layer_dirs(env)),
        *([str(_DXVK_NVAPI_LAYER_DIR)] if _DXVK_NVAPI_LAYER_DIR.exists() else []),
    ]
    if layer_paths:
        _prepend_path_entries(env, VK_LAYER_PATH_ENV, layer_paths, separator=":")


def _prepend_enabled_layers(env: dict[str, str]) -> None:
    layers = []
    if native_layer_dirs(env):
        layers.append(LATENCY_LAYER_NAME)
    if _DXVK_NVAPI_LAYER_DIR.exists():
        layers.append(_DXVK_NVAPI_LAYER_NAME)
    if layers:
        _prepend_path_entries(env, VK_LAYER_ENABLE_ENV, layers, separator=",")


def _prepend_path_entries(
    env: dict[str, str],
    key: str,
    entries: list[str],
    *,
    separator: str,
) -> None:
    existing = [item for item in str(env.get(key) or "").split(separator) if item]
    merged: list[str] = []
    for item in [*entries, *existing]:
        if item not in merged:
            merged.append(item)
    if merged:
        env[key] = separator.join(merged)


def _prepare_overlay_paths(env: dict[str, str]) -> None:
    for key in (OVERLAY_STATE_ENV, OVERLAY_TEXT_ENV):
        path = Path(str(env.get(key) or "")).expanduser()
        if not str(path):
            continue
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
