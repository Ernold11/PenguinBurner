"""The Game Perf Profile fingerprint badge a game's details header carries.

Hover or keyboard focus peeks the fingerprint; a click opens the Game Perf
Profile tab through ``on_open``. It appears only once a game has recorded
telemetry, for every launcher alike: rings are stored under the
``<launcher>:<id>`` game key.
"""

from __future__ import annotations

from collections.abc import Callable

from runtime.frame_history import (
    frame_history_file_stem,
    read_frame_history_for_game,
    summarize,
)

from ..dialogs.form_rows import wrapped_tooltip
from .game_perf_profile import (
    GamePerfProfilePeek,
    perf_profile_axes,
    perf_profile_pixmap,
    warming_text,
)

BADGE_SIZE = 40


class GamePerfProfileBadge:
    def __init__(self, *, QtCore, QtGui, QtWidgets, parent) -> None:
        self.QtCore = QtCore
        self.QtGui = QtGui
        self.QtWidgets = QtWidgets
        self._parent = parent
        #: on_open(game_key, game_name, target_fps) -- the window opens the tab.
        self.on_open: Callable[[str, str, float | None], None] | None = None
        self._game_key = ""
        self._game_name = ""
        self._target_fps: float | None = None
        self._summary = None
        self._power_limit_w = 0
        self._peek: GamePerfProfilePeek | None = None

        self.button = QtWidgets.QToolButton()
        self.button.setObjectName("gamePerfProfileBadge")
        self.button.setFixedSize(BADGE_SIZE + 6, BADGE_SIZE + 6)
        self.button.setIconSize(QtCore.QSize(BADGE_SIZE, BADGE_SIZE))
        self.button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.button.setVisible(False)
        self.button.setAccessibleName(
            "Game Perf Profile — open the Game Perf Profile tab"
        )
        self.button.clicked.connect(self._open)
        badge = self

        class _Hover(QtCore.QObject):
            """Hover or focus peeks the fingerprint; any click falls through
            to open the tab."""

            def eventFilter(self, watched, event):
                event_type = event.type()
                if event_type in (QtCore.QEvent.Type.Enter, QtCore.QEvent.Type.FocusIn):
                    badge._show_peek()
                elif event_type in (
                    QtCore.QEvent.Type.Leave,
                    QtCore.QEvent.Type.FocusOut,
                    QtCore.QEvent.Type.MouseButtonPress,
                    QtCore.QEvent.Type.Hide,
                ):
                    badge.hide_peek()
                return False

        self._hover_filter = _Hover(parent)
        self.button.installEventFilter(self._hover_filter)

    def _device_pixel_ratio(self) -> float:
        window = self._parent.window()
        handle = window.windowHandle() if window is not None else None
        return float(handle.devicePixelRatio()) if handle is not None else 1.0

    def sync(
        self, game_key: str = "", *, game_name: str = "", target_fps: float | None = None
    ) -> None:
        """Refresh for the selected game; hidden until it has recorded anything.

        A game that was never recorded gets no badge: an empty placeholder on
        every row says nothing, and the tab's empty state explains how to
        record one.
        """
        self._game_key = game_key if frame_history_file_stem(game_key) else ""
        self._game_name = game_name
        self._target_fps = target_fps
        history = read_frame_history_for_game(self._game_key) if self._game_key else None
        if history is None:
            self._summary = None
            self.hide_peek()
            self.button.setVisible(False)
            return
        summary = summarize(history)
        self._power_limit_w = history.header.power_limit_w
        ratio = self._device_pixel_ratio()
        if summary is not None and summary.qualified:
            self._summary = summary
            axes = perf_profile_axes(
                summary, target_fps=target_fps, power_limit_w=self._power_limit_w
            )
            pixmap = perf_profile_pixmap(
                self.QtCore, self.QtGui, axes=axes, tier=summary.tier,
                size=BADGE_SIZE, device_pixel_ratio=ratio,
            )
            self.button.setToolTip(
                wrapped_tooltip("Game Perf Profile — click to open the full view.")
            )
        else:
            self._summary = None
            self.hide_peek()
            pixmap = perf_profile_pixmap(
                self.QtCore, self.QtGui, axes=None, tier="",
                size=BADGE_SIZE, device_pixel_ratio=ratio,
            )
            minutes = summary.minutes if summary is not None else 0.0
            self.button.setToolTip(
                wrapped_tooltip(f"Game Perf Profile — {warming_text(minutes)}")
            )
        self.button.setIcon(self.QtGui.QIcon(pixmap))
        self.button.setVisible(True)

    def _show_peek(self) -> None:
        if self._summary is None:
            return
        if self._peek is None:
            self._peek = GamePerfProfilePeek(
                QtCore=self.QtCore,
                QtGui=self.QtGui,
                QtWidgets=self.QtWidgets,
                parent=self._parent,
            )
        below = self.button.mapToGlobal(self.button.rect().bottomRight())
        below.setY(below.y() + 6)
        self._peek.show_for(
            summary=self._summary,
            target_fps=self._target_fps,
            power_limit_w=self._power_limit_w,
            global_pos=below,
            device_pixel_ratio=self._device_pixel_ratio(),
            align_right=True,
        )

    def hide_peek(self) -> None:
        if self._peek is not None:
            self._peek.hide()

    def _open(self) -> None:
        self.hide_peek()
        if self._game_key and self.on_open is not None:
            self.on_open(self._game_key, self._game_name, self._target_fps)
