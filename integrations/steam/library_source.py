"""Steam seen through the launcher contract the game library tab speaks.

A thin read-only face over SteamIntegrationManager. Every write still goes
through that manager: it is the only thing that knows how to splice our wrapper
into %command% and how to take it back out again.
"""

from __future__ import annotations

from pathlib import Path

from common.flatpak_wrappers import ensure_steam_integration
from integrations.launchers.desktop_icons import desktop_icon
from integrations.launchers.library import (
    FIELD_CHOICE,
    FIELD_MULTILINE,
    FIELD_SWITCH,
    GROUP_COMMAND,
    GROUP_IN_GAME,
    WRITE_NEEDS_SETUP,
    WRITE_READY,
    LauncherBulkAction,
    LauncherField,
    LauncherWriteState,
    LibraryGame,
)

from .launch_options import injection_state
from .library import steam_playtime_hours
from .manager import SteamGameRow, SteamIntegrationManager
from .process import launch_steam_game, restart_steam
from .users import default_steam_root


class SteamLibrarySource:
    launcher_id = "steam"
    display_name = "Steam"
    #: Shipped fallback, used when the machine has no Steam icon of its own.
    icon_asset = "tab-steam.png"
    #: The icon Steam installs itself, preferred over our copy.
    desktop_icon_name = "steam"
    #: Steam exposes an API for starting a game; the tab offers Play for it.
    can_launch = True

    def __init__(
        self,
        manager: SteamIntegrationManager | None = None,
        *,
        home: Path | None = None,
        settings_path: str | Path | None = None,
    ) -> None:
        self.manager = manager or SteamIntegrationManager(
            home=home,
            settings_path=settings_path,
        )
        self._home = home
        self._rows: tuple[SteamGameRow, ...] = ()
        self._playtime: dict[str, float] = {}
        # Probed during refresh, which the tab runs off the GUI thread: pgrep
        # plus a CDP connect is seconds of work in the worst case, and every
        # selection change would otherwise pay it again.
        self._probe: dict[str, object] = {}

    def desktop_icon(self):
        """Steam's own installed icon, or None when it has none here."""
        return desktop_icon(self.desktop_icon_name, self._home)

    def available(self) -> bool:
        """Whether a Steam installation exists here.

        The install directory, not the `steam` binary on PATH: a library is
        worth listing and configuring whether or not the client can be
        launched, and PATH is the wrong question inside a Flatpak anyway.
        """
        return default_steam_root(self._home) is not None

    def refresh(self, *, deep: bool = True) -> None:
        # initialize_defaults stays off. Opening a tab must not write anyone's
        # settings file; the library scan is a read.
        #
        # The shallow pass skips reading every app's details back over CDP and
        # merges what the last deep pass cached instead. New installs and fresh
        # LastPlayed values still land, which is all the tab's timer needs.
        self._rows = self.manager.refresh(
            read_launch_options=deep,
            initialize_defaults=False,
        )
        self._playtime = steam_playtime_hours(self._home)
        self._probe = self._probe_state()

    def _probe_state(self) -> dict[str, object]:
        """The Steam-side facts every later question is answered from."""
        user = self.manager.active_user()
        return {
            "marker": bool(self.manager.marker_present()),
            "running": bool(self.manager.steam_running()),
            "cdp_ready": bool(self.manager.cdp_ready()),
            "user": getattr(user, "display_name", "") if user is not None else "",
        }

    def games(self) -> tuple[LibraryGame, ...]:
        return tuple(self._library_game(row) for row in self._rows)

    def _library_game(self, row: SteamGameRow) -> LibraryGame:
        return LibraryGame(
            launcher=self.launcher_id,
            game_id=row.game.app_id,
            name=row.game.name,
            subtitle=str(row.game.runtime_label or ""),
            last_played=int(row.game.last_played or 0),
            playtime_hours=float(self._playtime.get(row.game.app_id, 0.0)),
            art_path=row.game.icon_path,
            ready=bool(row.game.ready),
            wrapped=bool(injection_state(row.launch_options).wrapped),
            enabled=bool(row.setting.enabled),
            overlay=bool(row.setting.overlay),
            detail=row,
        )

    # -- what only Steam has -------------------------------------------------

    def fields(self, game: LibraryGame) -> tuple[LauncherField, ...]:
        row = game.detail
        if not isinstance(row, SteamGameRow):
            return ()
        return (
            self._latency_field(row),
            self._compat_tool_field(row),
            self._launch_options_field(row),
        )

    @staticmethod
    def _latency_field(row: SteamGameRow) -> LauncherField:
        """The markers Adaptive paces on, kept when the overlay is off.

        Steam takes this as a wrapper flag where Lutris takes an environment
        assignment -- see integrations/steam/launch_options.py -- but it is the
        same opt-in reaching the same wrapper, so the tab draws one row for it.
        """
        overlay_on = bool(getattr(row.setting, "overlay", False))
        return LauncherField(
            key="ingame_latency",
            kind=FIELD_SWITCH,
            title="Latency markers without the overlay",
            subtitle=(
                "Adaptive paces on these markers, and the launcher normally "
                "starts them with the overlay. Keep them when the overlay is "
                "off, or Adaptive loses base-frame pacing under frame generation."
            ),
            setter="set_game_ingame_latency",
            # With the overlay on the wrapper already runs the markers, so the
            # switch has nothing left to add: show it on, out of reach.
            value=True if overlay_on else bool(
                getattr(row.setting, "ingame_latency", False)
            ),
            enabled=not overlay_on,
            group=GROUP_IN_GAME,
        )

    def _compat_tool_field(self, row: SteamGameRow) -> LauncherField:
        """Proton, chosen through Steam itself.

        Only reachable while Steam is up and answering on CDP: the selection
        lives in Steam's own state, not in a file we could edit, so with the
        client down there is nothing to change and nothing to list.
        """
        live = bool(self._probe.get("cdp_ready"))
        # A game Steam calls native has nothing to pick a Proton for -- unless
        # the user already forced one, in which case they keep the way back.
        native = bool(getattr(row.game, "is_native_linux", False)) and not row.game.compat_tool
        choices: list[tuple[str, str]] = [("", self._compat_default_label(row))]
        if live:
            choices.extend(self.manager.available_compat_tools(row.game.app_id))
        selected = str(row.game.compat_tool or "")
        if selected and selected not in {name for name, _label in choices}:
            # Steam is using something we could not list -- a hand-installed
            # Proton build. Show it rather than silently reading as "default".
            choices.append((selected, selected))
        return LauncherField(
            key="compat_tool",
            kind=FIELD_CHOICE,
            title="Compatibility tool",
            subtitle=(
                "Keeps Steam's current choice by default. Selecting another "
                "entry uses Steam's own compatibility-tool setting for this game."
                if live and not native
                else "Steam reports this game as native Linux; it runs without "
                "a compatibility tool."
                if native
                else "Needs live apply: Proton is Steam's own setting, so the "
                "client has to be running and connected to change it."
            ),
            setter="set_game_compat_tool",
            value=selected,
            choices=tuple(choices),
            enabled=live and not native,
            group=GROUP_COMMAND,
        )

    @staticmethod
    def _compat_default_label(row: SteamGameRow) -> str:
        """What the empty choice means for this game, spelled out.

        "Steam default" alone tells the user nothing about what will actually
        run, so name the tool Steam would pick -- or say the game needs none.
        """
        if row.game.compat_tool:
            return "Steam default"
        effective = str(getattr(row.game, "effective_compat_tool_label", "") or "")
        if effective:
            return f"Steam default ({effective})"
        if getattr(row.game, "is_native_linux", False):
            return "Native Linux — no compatibility tool"
        return "Steam default"

    def _launch_options_field(self, row: SteamGameRow) -> LauncherField:
        return LauncherField(
            key="launch_options",
            kind=FIELD_MULTILINE,
            title="Command",
            subtitle="Steam launch options — %command% is where the game goes",
            setter="set_raw_launch_options",
            value=str(row.launch_options or ""),
            group=GROUP_COMMAND,
        )

    def write_state(self) -> LauncherWriteState:
        """Whether Steam will take a setting now, and what to press if not.

        Two things can stand in the way, and each has one button behind it: the
        integration was never set up, or it was but Steam has been running since
        before it was -- in which case Steam holds the launch options in memory
        and would write ours back out on exit.
        """
        probe = self._probe or self._probe_state()
        note = f"Steam user: {probe['user']}" if probe.get("user") else "Steam user: —"
        if not probe.get("marker"):
            return LauncherWriteState(
                state=WRITE_NEEDS_SETUP,
                summary="not set up",
                detail=(
                    "Find your installed games and connect them to "
                    "PenguinBurner for simple per-game profiles."
                ),
                note=note,
                action_label="Scan my Steam library",
                action="initialize",
            )
        if probe.get("running") and not probe.get("cdp_ready"):
            return LauncherWriteState(
                state=WRITE_NEEDS_SETUP,
                summary="read-only until initialized",
                detail=(
                    "Restart Steam once to finish connecting per-game profiles. "
                    "Your games and saved settings stay exactly as they are."
                ),
                note=note,
                action_label="Restart Steam to finish",
                action="restart",
                confirm="Cleanly shut down and relaunch the Steam client now?",
            )
        return LauncherWriteState(
            state=WRITE_READY,
            summary="live apply" if probe.get("running") else "Steam stopped",
            note=note,
        )

    def restart(self):
        """Restart the Steam client. Named here because write_state points at it."""
        return restart_steam()

    def bulk_actions(self) -> tuple[LauncherBulkAction, ...]:
        # ``affects`` and ``enabled_only`` are what let the tab grey out a
        # direction that would change nothing -- "enable all" when everything
        # already is -- without knowing what any of these actions mean.
        return (
            LauncherBulkAction(
                key="enable_all",
                label="Enable PenguinBurner for all games",
                setter="set_all_games_enabled",
                value=True,
                affects="enabled",
                confirm=(
                    "Add the PenguinBurner wrapper to the launch command of "
                    "{count} {games}?\n\nThe In-Game overlay stays off, and "
                    "MangoHud is disabled in wrapped games. \"Disable "
                    "PenguinBurner for all games\" restores each game's own "
                    "launch command."
                ),
            ),
            LauncherBulkAction(
                key="disable_all",
                label="Disable PenguinBurner for all games",
                setter="set_all_games_enabled",
                value=False,
                affects="enabled",
                confirm=(
                    "Remove the PenguinBurner wrapper from {count} {games} and "
                    "restore their own launch command?"
                ),
            ),
            LauncherBulkAction(
                key="overlay_all",
                label="Show In-Game overlay for enabled games",
                setter="set_all_games_overlay",
                value=True,
                affects="overlay",
                enabled_only=True,
                confirm=(
                    "Show the In-Game overlay in {count} PenguinBurner-enabled "
                    "{games}?"
                ),
            ),
            LauncherBulkAction(
                key="overlay_none",
                label="Hide In-Game overlay for all games",
                setter="set_all_games_overlay",
                value=False,
                affects="overlay",
                enabled_only=True,
                confirm="Hide the In-Game overlay in {count} {games}?",
            ),
        )

    # -- launching -----------------------------------------------------------
    #
    # Steam is the only launcher here that can start a game on our behalf, so
    # everything about it lives in this adapter rather than in the library tab.

    def launch(self, game_id: str) -> tuple[bool, str]:
        """Ask Steam to start a game. Returns (started, what to tell the user)."""
        try:
            ensure_steam_integration()
        except (OSError, RuntimeError) as error:
            return False, f"FAILED to repair the PenguinBurner Steam integration ({error})"
        if launch_steam_game(game_id):
            return True, "launching via Steam…"
        return False, "FAILED to launch (steam not available)"

    def stop(self, game_id: str) -> tuple[bool, str]:
        result = self.manager.stop_game(game_id)
        if getattr(result, "ok", False):
            return True, "stopping…"
        return False, f"FAILED to stop ({getattr(result, 'message', '')})"

    def running_game_ids(self) -> frozenset[str] | None:
        """Which of this launcher's games are running, or None if unknowable.

        None is not "nothing is running": it means the check itself failed, and
        the caller must hold every state rather than read a stalled probe as
        every game having exited.
        """
        return self.manager.running_game_ids()
