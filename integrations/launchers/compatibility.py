"""Reusable compatibility picker; launcher adapters own discovery and storage."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .library import FIELD_CHOICE, LauncherField


@dataclass(frozen=True)
class CompatibilityTool:
    value: str
    label: str
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CompatibilitySelection:
    value: str = ""
    label: str = ""
    default_label: str = "Launcher default"
    supported: bool = True


class CompatibilityBackend(Protocol):
    def discover(self) -> tuple[CompatibilityTool, ...]: ...
    def selection(self, game: Any) -> CompatibilitySelection: ...
    def write(self, game: Any, tool: CompatibilityTool | None) -> None: ...


class CompatibilityTools:
    """Cache discovery off the GUI thread and validate again before a write."""

    def __init__(self, backend: CompatibilityBackend, *, guidance: str) -> None:
        self.backend = backend
        self.guidance = guidance
        self._tools: tuple[CompatibilityTool, ...] = ()
        self._error = "Rescan to discover compatibility tools."

    def refresh(self) -> None:
        try:
            self._tools = self.backend.discover()
            self._error = ""
        except (OSError, ValueError, TypeError, RuntimeError) as error:
            self._tools = ()
            self._error = str(error)

    def field(self, game: Any) -> LauncherField:
        error = self._error
        try:
            selected = self.backend.selection(game)
        except (OSError, ValueError, TypeError, RuntimeError) as problem:
            selected = CompatibilitySelection()
            error = str(problem)
        choices = [("", selected.default_label)]
        choices.extend(
            (tool.value, selected.label if tool.value == selected.value and selected.label else tool.label)
            for tool in self._tools
        )
        if selected.value and selected.value not in {value for value, _ in choices}:
            choices.append((selected.value, f"{selected.label or selected.value} (not listed)"))
        return LauncherField(
            key="compat_tool", kind=FIELD_CHOICE, title="Compatibility tool",
            setter="set_game_compat_tool", value=selected.value, choices=tuple(choices),
            enabled=selected.supported and not error,
            subtitle=("This game does not use Wine or Proton." if not selected.supported
                      else error or self.guidance),
        )

    def save(self, game: Any, value: str) -> str:
        """Raise on refusal; return user-facing next-launch instructions on success."""
        if not isinstance(value, str):
            raise TypeError("Invalid compatibility tool selection.")
        if not self.backend.selection(game).supported:
            raise ValueError("This game does not use Wine or Proton.")
        tool = None
        if value:
            # A displayed build can be removed while the panel is open.
            self.refresh()
            if self._error:
                raise ValueError(self._error)
            tool = next((item for item in self._tools if item.value == value), None)
            if tool is None:
                raise ValueError("Selected compatibility tool is unavailable. Rescan the library.")
        self.backend.write(game, tool)
        if self.backend.selection(game).value != value:
            raise ValueError("The launcher did not retain the compatibility setting. Close its settings window and retry.")
        return f"Compatibility tool saved. {self.guidance}"
