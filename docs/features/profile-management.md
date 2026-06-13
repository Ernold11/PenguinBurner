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

- **Apply Selected** — run the highlighted profile now.
- **Apply Adaptive** — let the daemon switch tiers at runtime (see
  [adaptive-uv.md](./adaptive-uv.md)).
- **Silent fan curve** / **Persist on Startup** — toggle fan control and autostart.
- **Remove Autostart Entry** — stop applying a profile at boot.

Right-click a profile:

- **Edit VF Curve** / **Edit Fan Curve** — open the editors (see
  [curve-editor.md](./curve-editor.md)).
- **Apply** / **Verify** — apply the curve, or re-run verification.
- **Export LACT** — write the curve as a LACT config.
- **Assign Tier** — Efficiency / Balanced / Performance / None.
- **Delete** — remove the profile.

## Where profiles live

Saved profiles are stored under the PenguinBurner user config directory in
`auto-uv-profiles/`. Only final, verified (or user-edited) curves appear here.
