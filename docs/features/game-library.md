# Game Library

Manage **Steam, Lutris and Heroic** games in one list. Choose a GPU profile, Adaptive
FPS target, and overlay settings for each game, then launch it here or through
its usual launcher.

![Steam and Lutris games in Game Library](../assets/game-library.png)

## Setup

1. Open **Game Library** and let it discover installed games.
2. Select a game and enable **Wrap this game**.
3. Choose **Adaptive**, **Efficiency**, **Balanced**, **Performance**, or **Stock**.
4. Use **Play** to start the game; **Stop** appears while it runs.

Steam may require a one-time library scan or restart before edits are available.
See [Steam setup](../steam.md), [Lutris setup](lutris.md) or
[Heroic setup](heroic.md).
Discovery reads the library; wrapping a game changes its launch command.

## Per-game settings

| Setting | Effect |
| --- | --- |
| Wrap this game | Add the PenguinBurner wrapper; disabling it restores the original launch command. |
| Graphics card | Choose the NVIDIA GPU to tune; shown only on multi-GPU systems. |
| Auto-UV mode | Switch tiers with Adaptive, pin one tier, or use stock settings. |
| Per-game target | Override the system-wide Adaptive FPS target; off follows that target. |
| Overlay | Show the in-game HUD. Adaptive keeps required timing markers active when the HUD is hidden. |
| Command | View or edit the launch command, Lutris prefix, or Heroic wrapper row. |
| Compatibility tool | Choose the Wine/Proton version for a Windows game in Steam, Heroic, or native Lutris. Changes take effect on the next launch. |

The compatibility picker uses the launcher's available tools. **Heroic default**
and **Lutris default** remove the per-game override and restore inherited settings.
Install additional builds through the launcher's own version manager, then select
**Rescan** in Game Library. Lutris also offers its managed **GE-Proton (Latest)**
choice, which Lutris may download on launch. Native Linux games do not need a
compatibility tool.

Close the launcher's game settings window before changing versions here, so its
cached settings cannot overwrite your choice. For Heroic, use **Play** in Game
Library afterward: it refreshes an idle launcher when the version changes.
Both native and Flatpak Heroic are supported; the Flatpak version lists tools
visible inside its sandbox. A missing tool remains visible as the saved selection
but cannot be newly selected.

Existing launch options, command prefixes and wrapper rows are preserved.
Adaptive FPS targets update live for running wrapped Steam, Lutris, and Heroic
games, including switching back to the system-wide target. Tier changes still
wait for sustained frame-time headroom. Other Lutris changes apply on the next
launch. Heroic's Play action refreshes saved
launch settings automatically, restarting an idle launcher when necessary. Heroic overlay visibility also updates live
in an already wrapped game.
Steam supports live mode, target, and overlay updates
for a running wrapped game; changing its GPU requires a relaunch.

Changing a running wrapped Heroic or Lutris game's Auto-UV mode applies it live,
including switching between Adaptive and a fixed tier. PenguinBurner verifies
the running mode with the daemon before reporting success. If the live update
fails or cannot be confirmed, the selection remains saved for the next launch
and the status reports the problem; a saved choice alone is not a live readout.
Changing the target GPU still requires a relaunch.


## Library controls

Sort by launcher, name, recently played, or most played. **Rescan** refreshes
discovery without clearing settings. Scans and bulk writes run in the background.

The **All games** menu enables or disables wrapping, or shows or hides overlays
for enabled games across Steam, Lutris, and native or Flatpak Heroic. Bulk enable
preserves saved modes and overlay choices; new games default to overlay off.
Show skips games whose renderer cannot display the overlay. Other per-game
settings stay intact, and failures are reported while remaining games update.
Each action shows the affected count before confirmation. Overlay changes reach
already wrapped Steam and Heroic sessions live; Lutris uses them on next launch.

## Runtime behavior

The wrapper asks `penguin-burnerd` to apply the game profile, then restores your
standing profile when the game exits. If the daemon is unavailable, the game
still launches. Only one game can own the daemon's active monitoring and
Adaptive engine at a time; concurrent games do not replace that owner.

Game status follows wrapper registration and kernel process-exit events for
Steam, Lutris and Heroic. Opening Game Library during a game reconnects to the
current session snapshot. Launchers without complete notifications also receive
periodic recovery scans; a missed scan never proves that a game failed or that
PenguinBurner was absent.

**Starting…** means the launch was requested but a game session has not yet been
confirmed. **Retry launch…** asks before attempting another instance. A detected
external session shows **Running — PBurn unconfirmed** and disables Play. Missing
wrapper or GPU-profile evidence is a neutral status, not a failure warning.
Wrapper registration confirms the launch wrapper, not that the HUD has rendered.

If launch preparation fails before a request is sent, **Play** becomes available
again and other games remain launchable. A refused retry does not clear an earlier
launch whose status is still unknown. Errors after possible dispatch continue to
require **Retry launch…** confirmation.

If the daemon disconnects, the last known session is retained until recovery.
Reconnection only restores observation: it does not apply a skipped GPU profile
later. File-change notifications refresh library entries and saved settings;
background worker completion uses Qt signals. Bounded transport waits, reconnect
backoff, and periodic recovery scans remain; none is used to classify a launch
as failed or unwrapped.

For a diagnostic snapshot, run:

```bash
python -m runtime.daemon_client launcher-sessions
```

Fixed profiles work independently of the game's graphics API. Overlay and
Adaptive frame telemetry require Vulkan, including DXVK and vkd3d-proton.
Known native OpenGL games have those controls disabled; fixed profiles and
launching remain available.

See [Adaptive](adaptive-uv.md), [overlay controls](overlay.md), and
[latency and frame-generation FPS](latency-fg.md) for measurement details.
