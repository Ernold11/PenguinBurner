from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


# Clean command name resolved via PATH, so the Steam launch line stays readable
# instead of carrying a hardcoded /home/<user>/.local/bin/... path.
PENGUIN_BURNER_WRAPPER = "PENGUIN_BURNER"

# The only token any game needs in its launch line is the PenguinBurner wrapper;
# overlay and in-game latency are opt-in via extra tokens (see steam_game_setup).
DEFAULT_REQUIRED_TOKENS = (
    PENGUIN_BURNER_WRAPPER,
)


@dataclass(frozen=True)
class LaunchOptionsCheck:
    app_id: str
    config_path: Path | None
    launch_options: str | None
    required_tokens: tuple[str, ...]
    missing_tokens: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.launch_options is not None and not self.missing_tokens

    def format_text(self) -> str:
        lines = [
            f"app_id={self.app_id}",
            f"config={self.config_path or 'not-found'}",
            f"ok={self.ok}",
        ]
        if self.launch_options is None:
            lines.append("launch_options=not-found")
        else:
            lines.append(f"launch_options={self.launch_options}")
        if self.missing_tokens:
            lines.append("missing:")
            lines.extend(f"- {token}" for token in self.missing_tokens)
        return "\n".join(lines)


def default_localconfig_paths(home: Path | None = None) -> list[Path]:
    home = Path.home() if home is None else home
    candidates: list[Path] = []
    for root in (
        home / ".local" / "share" / "Steam" / "userdata",
        home / ".steam" / "steam" / "userdata",
    ):
        candidates.extend(root.glob("*/config/localconfig.vdf"))

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved not in seen:
            unique.append(path)
            seen.add(resolved)
    return unique


def _quoted_tokens(text: str) -> list[str]:
    return [match.group(1) for match in re.finditer(r'"([^"]*)"', text)]


def launch_options_from_localconfig(text: str, app_id: str) -> str | None:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if _quoted_tokens(line) != [app_id]:
            continue

        depth = 0
        entered_block = False
        for block_line in lines[index + 1 :]:
            stripped = block_line.strip()
            if stripped == "{":
                depth += 1
                entered_block = True
                continue
            if stripped == "}":
                if not entered_block:
                    break
                depth -= 1
                if depth <= 0:
                    break
                continue
            if not entered_block or depth <= 0:
                continue

            tokens = _quoted_tokens(block_line)
            if len(tokens) >= 2 and tokens[0] == "LaunchOptions":
                return tokens[1]
    return None


def check_launch_options(
    *,
    app_id: str,
    required_tokens: tuple[str, ...],
    config_paths: list[Path] | None = None,
) -> LaunchOptionsCheck:
    paths = config_paths if config_paths is not None else default_localconfig_paths()
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        launch_options = launch_options_from_localconfig(text, app_id)
        if launch_options is None:
            continue
        missing = tuple(
            token for token in required_tokens if token not in launch_options.split()
        )
        return LaunchOptionsCheck(
            app_id=app_id,
            config_path=path,
            launch_options=launch_options,
            required_tokens=required_tokens,
            missing_tokens=missing,
        )

    return LaunchOptionsCheck(
        app_id=app_id,
        config_path=None,
        launch_options=None,
        required_tokens=required_tokens,
        missing_tokens=required_tokens,
    )
