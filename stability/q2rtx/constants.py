from __future__ import annotations

from pathlib import Path
import re


DEFAULT_DURATION_S = 60
DEFAULT_WIDTH = 2560
DEFAULT_HEIGHT = 1440
DEFAULT_HIDE_WINDOW = True
HIDDEN_WINDOW_POSITION = "32000,32000"
DEFAULT_DEMO_NAME = "auto"
DEFAULT_POLL_INTERVAL_S = 1.0
DEFAULT_SINGLE_PASS_TIMEOUT_S = 120.0
DEFAULT_LOG_DIR = Path.home() / ".config" / "PenguinBurner" / "stability-logs"
DEFAULT_INSTALL_DATA_DIR = Path.home() / ".local" / "share" / "PenguinBurner" / "q2rtx"
DEFAULT_INSTALL_CACHE_DIR = Path.home() / ".cache" / "PenguinBurner" / "q2rtx"

Q2RTX_RELEASES_API_URL = "https://api.github.com/repos/NVIDIA/Q2RTX/releases/latest"
Q2RTX_GITHUB_RELEASES_URL = "https://github.com/NVIDIA/Q2RTX/releases"
OPENSSL_111_VERSION = "1.1.1w"
OPENSSL_111_COMPAT_RPM_INDEX_URL = (
    "https://mirror.stream.centos.org/9-stream/AppStream/x86_64/os/Packages/"
)
OPENSSL_111_REQUIRED_LIBS = ("libssl.so.1.1", "libcrypto.so.1.1")
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
    "No game data files detected",
    "Couldn't read demo",
    "Couldn't load maps/",
    "Couldn't open demos/",
    "Couldn't open pics/",
    "Couldn't load pics/",
    "error while loading shared libraries",
)

TIMEDEMO_LINE_RE = re.compile(
    r"(?P<frames>\d+)\s+frames,\s+(?P<seconds>[0-9.]+)\s+seconds:\s+(?P<fps>[0-9.]+)\s+fps"
)
