# Profile Management

> Feature guide — see the [README](../../README.md) for the project overview.

The **Profiles** tab lists every saved undervolt profile and is where you apply,
verify, tier, export, and clean up curves.

![Stored undervolt profiles](../assets/profiles-management.png)

## The table

Each row shows: Date, Profile name, mV, Target MHz, Effective MHz, FPS/W, FPS,
Power W, and Memory offset. Sort by any column to compare runs.

## Actions

Top bar:

- **Apply** — run the highlighted profile now.
- **Apply on startup** — also save the applied profile as the boot profile.
  Off by default: with it unticked, Apply changes the current session only
  and clears any saved boot profile, so the GPU starts at stock.
- **Silent fan curve** — use the saved fan curve with the applied profile.
- **Restore defaults** — return the GPU to stock now and at boot.

With **Apply on startup** ticked, Apply saves the selected profile as the
standing boot state. Restore defaults saves stock as that state instead. The
`penguin-burnerd` service stays enabled and available in all cases. The toggle
itself is remembered in `~/.config/PenguinBurner/penguin_burner.toml`
(`[ui] persist_on_startup`) and survives reinstalls and upgrades.

Right-click a profile:

- **Edit VF Curve** / **Edit Fan Curve** — open the editors (see
  [curve-editor.md](./curve-editor.md)).
- **Apply** / **Verify** — apply the curve, or re-run verification.
- **Export LACT** — write the curve as a LACT config.
- **Assign Tier** — Efficiency / Balanced / Performance / None.
- **Delete** — remove the profile.

Apply, Verify, and Delete go through the root hardware service
(`penguin-burnerd`), so none of them ask for your password; verification runs
as your regular user.

## Suspend/resume

An applied profile survives system sleep automatically. Waking from
suspend can silently reset driver state (power limit, locked clocks, the
V/F curve), so the runtime engine detects every resume, waits a few
seconds for the driver to settle, then re-verifies the applied profile and
reapplies anything that drifted. The check is read-first: nothing is
rewritten when the state survived. Detection works on any init system —
it compares two kernel clocks instead of listening to logind — and the
result is logged as `event=resume-reverify-complete` in the engine log.

## Where profiles live

Saved profiles are stored under the PenguinBurner user config directory in
`auto-uv-profiles/`. Only final, verified (or user-edited) curves appear here.
