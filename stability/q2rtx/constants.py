from __future__ import annotations

from pathlib import Path
import re


DEFAULT_DURATION_S = 60
DEFAULT_WIDTH = 3840
DEFAULT_HEIGHT = 2160
DEFAULT_DEMO_NAME = "auto"
DEFAULT_POLL_INTERVAL_S = 1.0
DEFAULT_SINGLE_PASS_TIMEOUT_S = 120.0
# Abort a pass if the benchmark's frame counter stops advancing for this long
# while the process is still alive (a frozen GPU). Deliberately generous - ~20x
# the 250 ms frame heartbeat and well beyond any plausible frame-time hitch or
# lazy pipeline compile - because a trip blacklists the voltage with no retry,
# so only an indefinite freeze must reach it. The pass timeout still catches
# anything slower. 0 disables; only arms after steady rendering is confirmed, so
# older binaries that never emit frame heartbeats are unaffected.
DEFAULT_HANG_WATCHDOG_S = 5.0
DEFAULT_LOG_DIR = Path.home() / ".config" / "PenguinBurner" / "stability-logs"
DEFAULT_INSTALL_DATA_DIR = Path.home() / ".local" / "share" / "PenguinBurner" / "q2rtx"
DEFAULT_INSTALL_CACHE_DIR = Path.home() / ".cache" / "PenguinBurner" / "q2rtx"

PB_Q2RTX_REPOSITORY = "jpietek/Q2RTX-headless"
PB_Q2RTX_LATEST_RELEASE_API_URL = (
    f"https://api.github.com/repos/{PB_Q2RTX_REPOSITORY}/releases/latest"
)
PB_Q2RTX_ASSET_PREFIX = "q2rtx-penguinburner-"
PB_Q2RTX_ASSET_SUFFIX = "-linux-x86_64.tar.gz"

Q2RTX_SHAREWARE_DATA_VERSION = "1.8.1-shareware-data"
Q2RTX_SHAREWARE_DATA_ASSET_NAME = "q2rtx-1.8.1-linux.tar.gz"
Q2RTX_SHAREWARE_DATA_ASSET_URL = (
    "https://github.com/NVIDIA/Q2RTX/releases/download/"
    f"v1.8.1/{Q2RTX_SHAREWARE_DATA_ASSET_NAME}"
)
Q2RTX_REQUIRED_DATA_FILES = (
    "baseq2/pak0.pak",
    "baseq2/blue_noise.pkz",
    "baseq2/q2rtx_media.pkz",
    "baseq2/shaders.pkz",
)
PAK_ENTRY_SIZE = 64
PREFERRED_AUTO_DEMO_NAMES = (
    "q2demo1",
    "demo1",
    "q2demo2",
    "demo2",
    "q2demo3",
    "demo3",
)
KNOWN_TIMEDEMO_FRAME_COUNTS = {
    "q2demo1": 631,
}
KNOWN_TIMEDEMO_RUN_SECONDS_HINTS = {
    "q2demo1": 4.45,
}

Q2RTX_BINARY_CANDIDATES = (
    "q2rtx",
    "build/q2rtx",
    "build/release/q2rtx",
    "build/Release/q2rtx",
    "bin/q2rtx",
)

FATAL_OUTPUT_PATTERNS = (
    "ERR_FATAL",
    "device lost",
    "Error during initialization",
    "Server fatal crashed",
    "recursive error after",
    "Segmentation fault",
    "Aborted",
    "core dumped",
    "Illegal instruction",
    "Bus error",
    "Floating point exception",
    "Trace/breakpoint trap",
    "assertion",
    "malloc() failed",
    "couldn't allocate",
    "out of memory",
    "failed to create Vulkan instance",
    "failed to create Vulkan device",
    "failed to create swapchain",
    "No game data files detected",
    "Couldn't read demo",
    "Couldn't load maps/",
    "Couldn't open demos/",
    "Couldn't open pics/",
    "Couldn't load pics/",
)
FATAL_OUTPUT_REGEXES = (
    re.compile(r"\bVK_ERROR_[A-Z0-9_]+\b", re.IGNORECASE),
    re.compile(r"\bSIG(?:SEGV|ABRT|BUS|ILL|FPE|TRAP|KILL)\b", re.IGNORECASE),
)

# Dynamic-loader failures mean Q2RTX never started: these are environment/launch
# errors, not GPU instability, and must never blacklist a voltage as unsafe.
LAUNCHER_ERROR_PATTERNS = ("error while loading shared libraries",)

# Reason assigned to a stability result whose Q2RTX process failed to launch.
Q2RTX_LAUNCHER_ERROR_REASON = "q2rtx-launcher-error"
