# Adaptive Undervolting

> Feature guide — see the [README](../../README.md) for the project overview and
> [auto-uv.md](./auto-uv.md) for how the underlying profiles are produced.

Adaptive Undervolting is PenguinBurner's standout runtime feature. Instead of
locking your GPU to a single saved curve, it keeps several saved Auto-UV
profiles on hand, assigns each one a **tier**, and lets the daemon switch
between tiers automatically based on how the game is actually pacing frames.

![Profiles with the Assign Tier menu](../assets/profiles-management.png)

## Tiers

Every saved profile can be tagged with one of three tiers from the **Profiles**
tab (right-click a profile → **Assign Tier**) or from the CLI:

| Tier | Intent | Auto-UV tail-rise bins |
| --- | --- | --- |
| **Efficiency** | Lowest practical power, flat post-lock curve | `0` |
| **Balanced** | Moderate clock tail | `≥ 4` |
| **Performance** | Preserve clock, highest headroom | `≥ 6` |

Choose **None** to remove a tier assignment. The tier of a freshly generated
profile is inferred from the scan's performance-bias preset, so a normal
Efficiency / Balanced / Performance scan already lands in the matching tier.

CLI tier assignment uses profile ids from `--list-auto-uv-profiles`:

```bash
./penguin_burner.sh --assign-auto-uv-tier <eff-profile-id> efficiency
./penguin_burner.sh --assign-auto-uv-tier <bal-profile-id> balanced
./penguin_burner.sh --assign-auto-uv-tier <perf-profile-id> performance
./penguin_burner.sh --assign-auto-uv-tier <profile-id> none
```

## How runtime switching works

Enable it per game from the **Steam** tab (set the game's mode to
**Adaptive**), or on the command line with `--adaptive-auto-uv` in
runtime/daemon mode:

```bash
./penguin_burner.sh --daemonize --adaptive-auto-uv
```

For persistent boot autostart (full service install, root required):

```bash
sudo ./penguin_burner.sh --install-systemd-service --adaptive-auto-uv
```

PenguinBurner watches the **base present-frame p95 pacing** and compares it to a
target FPS. When frames are comfortably ahead of target it shifts toward a more
efficient tier (less power, quieter); when pacing falls behind it shifts toward
a higher tier to protect frame rate.

The target defaults to **60 FPS**. Override it for the service through the
environment variable (30, 50, 60, 120, etc.):

```bash
PENGUIN_BURNER_ADAPTIVE_TARGET_FPS=120
```

The same value can be set under `[adaptive] target_fps` in the runtime config
file, and the **Target FPS** control in the Overlay tab reflects it.

## Example

Say you keep three profiles (Efficiency, Balanced, Performance) and a 60 FPS
target:

- In a menu or a light scene, frames sit well above 60, so PenguinBurner runs
  the **Efficiency** profile: lowest power, quietest fans.
- The action picks up and frames start hovering near 60, so it moves to
  **Balanced** to hold the target.
- A heavy effect pushes frames below 60, so it jumps to **Performance** for the
  extra clock until the scene clears, then eases back down.

You set it once and play; the tier follows the frame rate.

## Requirements and safety

- Adaptive UV needs **at least two usable profile tiers** to have anything to
  switch between. With a single tier it simply runs that one profile.
- Deleting a profile that would drop you below two usable tiers triggers a
  warning, because it disables adaptive switching for the autostart entry.
- All per-tier curves still went through Auto-UV's stability and final
  verification before they could be saved, so every tier the daemon can pick is
  a previously verified curve.
