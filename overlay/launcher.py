from __future__ import annotations

import os
from pathlib import Path
import re
import sys

from .config import OVERLAY_CONFIG_ENV, default_overlay_config_path
from .native_layer import LATENCY_LAYER_NAME
from .native_layer import native_layer_dirs
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
    configure_penguin_burner_environment(env, command_args=args)
    _remove_mangohud_environment(env)
    _prepare_overlay_paths(env)
    if ingame_latency_enabled(env):
        _route_trace_to_fifo(env)
    os.execvpe(args[0], args, env)
    return 127


def trace_fifo_path(env: dict[str, str]) -> Path:
    return _home_latency_socket_path(env).with_name("nvapi-trace.fifo")


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
) -> None:
    overlay_config_path = default_overlay_config_path(env)
    env.setdefault(OVERLAY_CONFIG_ENV, str(overlay_config_path))
    env.setdefault(MASTER_ENABLE_ENV, "1")
    env.setdefault(LATENCY_ENABLE_ENV, "1")
    _apply_overlay_enable_alias(env)
    env.setdefault(OVERLAY_ENABLE_ENV, "auto")
    env.setdefault(DXVK_NVAPI_ENABLE_ENV, "1")
    env.setdefault("PROTON_ENABLE_NVAPI", "1")
    env.setdefault("PROTON_HIDE_NVIDIA_GPU", "0")
    if ingame_latency_enabled(env):
        _configure_dxvk_nvapi_marker_output(env, command_args=command_args)
        # Fold the present->scanout display tail into the same opt-in. Both are
        # read-only/gated in the layer and inert where unsupported.
        env.setdefault(DISPLAY_LATENCY_ENV, "1")
        env.setdefault(INJECT_PRESENT_ID_ENV, "1")
    env.setdefault(LATENCY_SOCKET_ENV, str(_home_latency_socket_path(env)))
    env.setdefault(OVERLAY_STATE_ENV, str(overlay_state_path(env)))
    env.setdefault(OVERLAY_TEXT_ENV, str(overlay_text_path(env)))
    _prepend_layer_paths(env)
    _prepend_enabled_layers(env)


def _configure_dxvk_nvapi_marker_output(
    env: dict[str, str],
    *,
    command_args: list[str] | tuple[str, ...] | None = None,
) -> None:
    # If the user already forced full trace, honor it and avoid enabling
    # marker-only logging too; emitting both formats would duplicate samples.
    if str(env.get(DXVK_NVAPI_TRACE_ENV) or "").strip().lower() == "trace":
        return

    if str(env.get(DXVK_NVAPI_MARKER_LOG_ENV) or "").strip().lower() in _TRUTHY:
        return

    if dxvk_nvapi_marker_log_supported(env, command_args=command_args):
        env.setdefault(DXVK_NVAPI_MARKER_LOG_ENV, "1")
    else:
        env.setdefault(DXVK_NVAPI_TRACE_ENV, "trace")


def dxvk_nvapi_marker_log_supported(
    env: dict[str, str],
    *,
    command_args: list[str] | tuple[str, ...] | None = None,
) -> bool:
    prefix_candidates = list(_prefix_nvapi_dll_candidates(env))
    if prefix_candidates:
        return any(_dll_contains_marker_log_flag(path) for path in prefix_candidates)

    return any(
        _dll_contains_marker_log_flag(path)
        for path in _proton_nvapi_dll_candidates(env, command_args=command_args)
    )


def _prefix_nvapi_dll_candidates(env: dict[str, str]):
    data_path = str(env.get("STEAM_COMPAT_DATA_PATH") or "").strip()
    if not data_path:
        return
    windows = Path(data_path).expanduser() / "pfx" / "drive_c" / "windows"
    yield from _existing_paths(
        (
            windows / "system32" / "nvapi64.dll",
            windows / "syswow64" / "nvapi.dll",
        )
    )


def _proton_nvapi_dll_candidates(
    env: dict[str, str],
    *,
    command_args: list[str] | tuple[str, ...] | None = None,
):
    roots: list[Path] = []
    for key in ("STEAM_COMPAT_TOOL_PATHS", "STEAM_COMPAT_TOOL_PATH"):
        value = str(env.get(key) or "").strip()
        if not value:
            continue
        for item in value.split(os.pathsep):
            if item:
                roots.append(Path(item).expanduser())

    for arg in command_args or ():
        try:
            path = Path(str(arg)).expanduser()
        except OSError:
            continue
        if path.name == "proton":
            roots.append(path.parent)

    seen: set[Path] = set()
    for root in roots:
        try:
            root = root.resolve()
        except OSError:
            pass
        if root in seen:
            continue
        seen.add(root)
        yield from _nvapi_dlls_under_proton_root(root)


def _nvapi_dlls_under_proton_root(root: Path):
    yield from _existing_paths(
        (
            root / "files" / "lib" / "wine" / "nvapi" / "x86_64-windows" / "nvapi64.dll",
            root / "files" / "lib" / "wine" / "nvapi" / "i386-windows" / "nvapi.dll",
            root
            / "files"
            / "lib"
            / "wine"
            / "nvidia-libs"
            / "nvapi"
            / "x86_64-windows"
            / "nvapi64.dll",
            root
            / "files"
            / "lib"
            / "wine"
            / "nvidia-libs"
            / "nvapi"
            / "i386-windows"
            / "nvapi.dll",
        )
    )


def _existing_paths(paths):
    for path in paths:
        try:
            if path.is_file():
                yield path
        except OSError:
            continue


def _dll_contains_marker_log_flag(path: Path) -> bool:
    needle = DXVK_NVAPI_MARKER_LOG_ENV.encode("ascii")
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                if needle in chunk:
                    return True
    except OSError:
        return False
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
