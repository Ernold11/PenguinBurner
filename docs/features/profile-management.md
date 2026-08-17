# Profile Management

> Feature guide — see the [README](../../README.md) for the project overview.

The **Profiles** tab lists every saved undervolt profile and is where you apply,
verify, tier, export, and clean up curves.

![Stored undervolt profiles](../assets/profiles-management.png)

## The table

Each row shows: Date, Profile name, GPU, mV, Target MHz, Effective MHz, FPS/W,
FPS, Power W, and Memory offset. Sort by any column to compare runs. Verified
profiles retain the GPU name, UUID, and PCI identity from verification, so a
saved curve cannot silently move to another card if driver indices change.

## Actions

Top bar:

- **Apply** — run the highlighted profile now.
- **Target GPU** — filter profiles and choose the physical card for actions.
  The selector remains visible but disabled when only one GPU is detected.
- **Apply on startup** — also save the applied profile for the selected GPU.
  Off by default: with it unticked, Apply changes the current session only
  and clears only that GPU's saved boot profile.
- **Silent fan curve** — use the saved fan curve with the applied profile.
- **Restore defaults** — return the GPU to stock now and at boot.

On a one-GPU system, the disabled target selector identifies the card and
**Apply** works as before. On a multi-GPU system, selecting a target filters out
profiles bound to other cards; legacy/unassigned profiles remain visible so
they can be verified and bound. Tier assignments and the startup checkbox are
kept per GPU. A legacy profile can be used directly on a one-GPU system; on a
multi-GPU system it must be verified on the intended card first.

With **Apply on startup** ticked, Apply saves the selected profile in that
GPU's boot entry. At boot, `penguin-burnerd` resolves saved UUIDs to their
current driver indices and applies the available entries serially. A missing
GPU is skipped but remains saved for a later boot. Restore defaults saves stock
for the selected GPU instead.

The Rust daemon still has one active policy engine. After serial application,
the most recently saved available GPU remains actively monitored and gets
drift recovery, adaptive switching, and PenguinBurner fan control. Earlier
GPUs keep their V/F curve, memory offset, and power limit, while their fans are
released to hardware auto. Selecting and applying another GPU transfers the
active engine to it; it does not run a second monitoring engine.

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

While a profile runtime is active, it survives system sleep
automatically. Waking from suspend can silently reset driver state
(power limit, locked clocks, the V/F curve), so the runtime engine
detects resumes from sleeps longer than a couple of seconds, waits a few
seconds for the driver to settle, then re-asserts the applied profile:
persistence policy is re-asserted, the power limit is checked and only
rewritten when it drifted, the clock ceiling is re-locked, fan state is
re-asserted, and the V/F curve re-verifies through the engine's usual
drift guard. Detection works on any init system — it compares two kernel
clocks instead of listening to logind — and the result is logged as
`event=resume-reverify-complete` in the engine log (or
`event=resume-reverify-gave-up` if the driver kept rejecting the
recovery writes; the per-tick guards keep re-verifying from there).

This covers the runtime engine only: state that was deliberately left on
the GPU after stopping the runtime is not re-verified after a sleep —
reapply the profile if you suspend in that state.

## Where profiles live

Saved profiles are stored under the PenguinBurner user config directory in
`auto-uv-profiles/`. Only final, verified (or user-edited) curves appear here.
