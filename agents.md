# PenguinBurner Agent Recipe

This repository should stay readable by feature boundary. Before adding a new
feature, decide which user-facing capability owns it, then keep the code inside
that boundary unless there is a clear shared contract.

## Current Boundaries

- `auto_uv/`: Auto-UV scan/search logic, scan modes, candidate selection,
  final verification orchestration, and Auto-UV persistence helpers.
- `stability/`: managed stability workloads and their install/runtime helpers,
  currently Q2RTX plus the CUDA companion load.
- `runtime/`: the daemon socket client (`daemon_client.py`), the systemd-unit
  installer (`support/runtime_service.py`), and scan/verify support modules
  (`gpu_control/`, `stability_test/`, `support/`). The root daemon itself is the
  Rust `burnerd/` crate — it applies saved profiles, fan control, adaptive tier
  switching, and live GPU policy enforcement in-process.
- `profiles/`: saved Auto-UV profile storage, verification, tier assignment, and
  runtime profile payload interpretation.
- `overlay/`: Steam wrapper, overlay config, native Vulkan layer, compact text,
  and latency telemetry receiver/parser.
- `ui/`: Qt GUI, organized by visible user features under `ui/features/` and
  reusable widgets under `ui/components/`.
- `cli/`: CLI argument parsing and top-level routing. Keep this narrow: Auto-UV
  scans, profile apply/verify, daemon install/remove, and status-like commands.
- `integrations/`: external format boundaries, currently Afterburner import and
  LACT export.
- `drivers/`: low-level NVIDIA/NVML/NvAPI access. Do not put product policy
  here.
- `curve_editors/`: manual fan/VF curve editing logic.
- `common/`: small shared helpers with no product ownership.
- `docs/`: user-facing docs only. Development specs belong outside tracked docs
  or in ignored scratch space.

## Cleanup Branch Lessons

These rules come from the `cleanup` branch refactor history. Do not reintroduce
the old shapes just because they look convenient for a local change.

Product surface:

- Keep the CLI narrow. It exists for Auto-UV scans, applying verified profiles,
  verification, daemon install/remove, and status-oriented runtime work. Do not
  bring back broad preset-specific CLI UX, Steam wait loops, automatic writes as
  default behavior, RE9/dev experiment knobs, or standalone console scripts
  unless the user explicitly asks for a debugging surface.
- CLI Auto-UV options must mirror GUI-visible Auto-UV preset options. If the GUI
  exposes target voltage, clock-drop percent, memory offset, power limit, or
  preset-specific efficiency/performance fields, the CLI path may carry them.
  Hidden tuning flags are not acceptable.
- Keep plain Afterburner import and LACT export because they are visible GUI
  integrations. Simplification should route imported payloads into normal saved
  profile flows, not create separate runtime-only profile formats.
- Keep Q2RTX as the managed stability workload for Auto-UV/final verification.
  Do not turn it back into a standalone product surface. Do not remove Q2RTX
  download/install pieces unless the managed binary and required `.pak` data path
  are still covered and tested.
- Do not reintroduce the Qt overlay window fallback. The in-game overlay path is
  the native Vulkan layer plus daemon-published state.

Directory structure:

- High-level user/product components define top-level directories:
  `auto_uv/`, `runtime/`, `profiles/`, `overlay/`, `ui/`, `integrations/`,
  `drivers/`, `curve_editors/`, `cli/`, and `common/`.
- Group by responsibility, not by implementation accident. Examples from the
  cleanup branch:
  - native layer and telemetry moved under `overlay/`
  - daemon fan/GPU/support/stability helpers moved under `runtime/`
  - saved profiles and verification moved under `profiles/`
  - Afterburner and LACT moved under `integrations/`
  - NVML/NvAPI helpers moved under `drivers/nvidia/`
  - manual curve editors moved under `curve_editors/`
  - Auto-UV algorithm/run/support modules moved under `auto_uv/`
  - Q2RTX/CUDA stability workloads moved under `stability/`
  - Qt workflow logic moved under `ui/features/`
- When moving a directory, update all four surfaces in the same commit: imports,
  package metadata, tests, and docs/help/error strings.

Imports and public surface:

- Avoid package-root compatibility facades. The cleanup removed lazy re-export
  maps from roots such as `profiles.uv`, `runtime.gpu_control`, and
  `stability.q2rtx`. Do not add them back.
- An import that looks unused may be an accidental re-export. Do not preserve
  that pattern. Move consumers to the real owner module, then remove the
  re-export.
- Tests must not rely on accidental re-exports either. If a test needs
  `FlattenedClockCeilingController`, import it from
  `runtime.gpu_control.flattened_clock_ceiling`; if it needs socket helpers,
  import from `overlay.telemetry.sockets`.
- After any move, scan for stale module paths in source, tests, CLI help, docs,
  and user-facing error messages. A move is incomplete if old command text such
  as `python -m auto_uv.stability.q2rtx` survives in help or error output.

Runtime and daemon behavior:

- Foreground Auto-UV may stop the current service so the scan can own the GPU,
  but it must not disable the persistent service. `systemctl stop` is acceptable;
  `systemctl disable --now` is not a foreground-scan side effect.
- Do not make automatic writes the default path. Persist/apply/install only when
  the user chose that action.
- Adaptive Auto-UV depends on verified saved profile tiers and live telemetry.
  Do not judge or change adaptive behavior without checking the daemon log and
  the telemetry path.
- Target FPS belongs to overlay/runtime config with explicit env override
  support. Do not scatter separate target-FPS storage.

Overlay and latency:

- Keep Python overlay text and native overlay text in sync. Field ordering,
  missing-latency handling, FPS/base-FPS semantics, profile tier labels, and unit
  strings must match across both paths.
- Latency is optional marker-derived telemetry, not NVML telemetry. Missing
  latency should not break the overlay and should not be displayed as noisy fake
  data.
- Overlay packaging must be verified after directory moves. The wheel must still
  include `overlay/native_layer/libVkLayer_penguinburner_latency.so` and
  `overlay/native_layer/VkLayer_PENGUINBURNER_latency.json`.
- A rebuilt native layer does not update a running game. Relaunch the game
  before claiming live overlay behavior is fixed.

Docs and specs:

- Keep tracked docs user-facing. Past design notes, implementation plans, and
  one-off specs belong in ignored scratch space, not in shipped docs.
- If code behavior changes, update `readme-cli.md`, feature docs, release-note
  snippets, and help/error text in the same change.

Dead code cleanup:

- Use `vulture`, `ruff`, `scc`, `cloc`, and `tokei`, but do not delete every
  static hit blindly. Confirm dynamic Qt callbacks, CLI entry points, package
  data paths, and subprocess/module entry points before removal.
- Remove dead leaves before splitting big active modules. That keeps later
  refactors smaller and reduces false dependencies.
- Do not bring back ASCII chart/curve output helpers for CLI Auto-UV unless the
  CLI actually renders them. The CLI should report each Auto-UV step clearly in
  text/table progress, not maintain unused drawing code.

Known regression patterns from the cleanup:

- Stale moved-module strings survive in help/error text even when imports pass.
- Package-root `__init__.py` barrels hide ownership and make future refactors
  harder.
- Ruff can identify accidental re-exports; fix consumers instead of keeping the
  re-export alive.
- Tests often encode old import surfaces. Update tests to the new ownership
  boundary rather than restoring compatibility aliases.
- Native/packaging moves can pass unit tests but fail in the installed wheel.
- Unit tests cannot prove live GPU clocks, voltage, power, latency, overlay, or
  adaptive switching. State clearly when live validation was not run.

## Feature Recipe

1. Start from the visible workflow.
   - GUI option? Add it to the relevant `ui/features/*` owner and persist it
     through the runtime/config module that already owns that state.
   - CLI option? Only add it if the same Auto-UV/runtime behavior is visible in
     the GUI or is needed for daemon/profile operation.
   - Daemon behavior? Put policy in `runtime/`, not in the UI or CLI.
   - Overlay behavior? Keep Python/native overlay formatting aligned and update
     both paths when visible text changes.

2. Keep imports explicit.
   - Import from concrete modules, for example `profiles.uv.profile_store`, not
     from package-root facades such as `profiles.uv`.
   - Do not add lazy re-export maps in `__init__.py`. Package roots should be
     minimal and not hide ownership.

3. Keep options aligned with the GUI.
   - Do not add hidden Auto-UV CLI tuning flags that are not exposed in the GUI.
   - Do not remove CLI options that represent GUI-visible Auto-UV preset fields.
   - Shared profile options such as power limit and memory offset must stay
     available wherever GUI presets expose them.

4. Avoid per-game product logic.
   - New tuning behavior should be generic.
   - Q2RTX is allowed as the managed stability workload for Auto-UV/final
     verification, not as a template for hardcoded game behavior.
   - Do not bury workload UI or runner code inside `auto_uv/`; keep the
     algorithm package focused on choosing and verifying voltage candidates.

5. Keep writes explicit.
   - Do not make automatic writes the default behavior unless the user chose an
     apply/save/install action.
   - Foreground Auto-UV may stop a running service, but must not disable the
     persistent service as a side effect.

6. Preserve integrations users can see.
   - Keep plain Afterburner import available in the GUI.
   - Keep LACT export available in the GUI.
   - If integration payload format changes, update import/export tests and user
     docs in the same change.

7. Treat latency and overlay as live-system features.
   - The in-game overlay is the native layer path; do not reintroduce the Qt
     overlay window fallback.
   - Latency can be unavailable in a game. Missing latency should be omitted or
     represented cleanly, not treated as a crash.
   - Do not break the daemon telemetry publisher or adaptive latency inputs when
     simplifying overlay code.

8. Adaptive Auto-UV uses saved profile tiers.
   - Tier assignment belongs in `profiles/uv/profile_tiers.py`.
   - Runtime switching belongs in the Rust daemon (`burnerd/src/profile/adaptive.rs`).
   - The controller needs at least two usable verified tiers.
   - Target FPS comes from overlay/runtime config with env override support.

9. Keep stability testing scoped.
   - Stability workloads live in `stability/` and are invoked by Auto-UV scans
     and final verification.
   - Do not add a separate product surface for standalone stress testing unless
     the user explicitly asks for it.

10. Preserve preset-specific Auto-UV behavior.
    - Efficiency has a low-voltage descent stage and a later tail-tune pass;
      do not flatten that into the Balanced/Performance defaults. The tail-tune
      pass intentionally raises the allowed tail-rise bins by 2.
    - Balanced and Performance can share sweep machinery, but keep their
      user-visible defaults and Performance Auto-OC behavior distinct.

11. Update all user surfaces together.
    - Code, GUI labels, CLI help, `readme-cli.md`, feature docs, release notes,
      and targeted tests should agree.
    - A rename is not complete while stale names remain in docs, errors, help
      text, or tests.

## Review Checklist

Before merging a feature or cleanup:

1. For every new feature/functionality commit, run the static-analysis routine.
   Do not skip this because the change is "small"; the refactor showed that
   stale imports, docs/help drift, and hidden facade dependencies are cheap to
   catch early.
   ```bash
   scripts/check-feature-static-analysis.sh
   ```

2. Run a stale import/path scan for moved modules when the feature renames or
   relocates anything.
   ```bash
   rg -n "old_module_or_flag_name" --glob '*.py' --glob '*.md'
   ```

3. Compile active Python packages if the full routine cannot be run for some
   external reason. This is the minimum fallback, not a replacement.
   ```bash
   PYTHONPATH=. python3 -m compileall -q \
     auto_uv cli common curve_editors drivers integrations overlay profiles runtime ui penguin_burner.py
   ```

4. Run focused tests for the touched boundary, then the full suite.
   ```bash
   PYTHONPATH=. pytest -q tests/path_or_file.py
   PYTHONPATH=. pytest -q
   ```

5. For packaging or directory moves, build the wheel and verify included assets.
   ```bash
   CIBW_CONTAINER_ENGINE=podman scripts/build-python-dist.sh dist/python
   ```

6. For overlay/native changes, verify the native layer artifacts are in the
   wheel and do a live game relaunch before claiming runtime behavior is fixed.

7. For daemon/adaptive changes, verify with logs from the real service boundary.
   ```bash
   journalctl -u penguin-burnerd
   ```

8. For Auto-UV behavior, run or explicitly defer live hardware validation. Unit
   tests are not enough to prove clocks, voltage, power, latency, or adaptive
   switching in a real game.

## Mandatory Static Analysis Routine

No agent may commit new code or new functionality without running
`scripts/check-feature-static-analysis.sh` first. This is mandatory for every
feature commit, refactor commit, and behavior-changing cleanup. If a tool cannot
run, stop and explain the blocker instead of committing.

The routine encodes the tools used during the cleanup/refactor:

- `rg`: stale moved-package imports, old names, package-root facade imports, and
  docs/help drift.
- `python -m compileall`: syntax/import-time bytecode validation for active
  packages.
- packaged import smoke test: imports every package/module declared in
  `pyproject.toml`.
- `ruff`: strict Python lint gate. Keep this clean.
- `vulture`: dead-code scan. Treat results seriously, but confirm each hit
  before deleting because dynamic Qt callbacks and CLI entry points can look
  unused.
- `pyright`: run every time, but advisory until the repo has a clean baseline.
  Use `PB_STATIC_STRICT_PYRIGHT=1 scripts/check-feature-static-analysis.sh` only
  after the touched area is typed enough to make Pyright a real gate.
- `scc`: LOC and complexity summary for active code.
- `cloc`: independent LOC cross-check.
- `tokei`: fast LOC cross-check.
- `git diff --check`: whitespace and patch hygiene.

Before committing, paste the relevant static-analysis result summary into the
commit/PR notes: hard gates passed, Pyright advisory status, and whether LOC or
complexity moved meaningfully. A new code commit without this routine is not
acceptable.

## Structure Rules

Directory structure:

- Put code in the directory that owns the visible workflow. Do not create a new
  top-level package for a narrow helper.
- If a module grows because it now owns two different responsibilities, split by
  responsibility before adding more branches.
- Keep user feature logic under `ui/features/*`; keep reusable widgets under
  `ui/components/*`.
- Keep product policy out of `drivers/`; driver modules should expose hardware
  facts and operations only.
- Keep daemon/runtime policy out of `cli/`; CLI routes into runtime owners.
- Keep external format translation in `integrations/`, not in profile/runtime
  code.

Import structure:

- Use concrete module imports. Example:
  `from profiles.uv.profile_store import resolve_auto_uv_profile`.
- Do not import from convenience package roots such as `profiles.uv`,
  `runtime.gpu_control`, or `stability.q2rtx`.
- Do not add `__init__.py` re-export barrels or lazy `__getattr__` maps.
- After moving code, run the facade import scan from the static routine.

Function structure:

- Prefer small pure helpers for parsing, normalization, scoring, formatting, and
  path resolution.
- Keep side effects at the edge: UI handlers, CLI routing, daemon apply loops,
  file writes, hardware writes, and subprocess launches.
- Do not mix UI rendering, persistence, and hardware mutation in one function.
- Do not add flags or parameters "just in case". Add the narrow option visible
  in the GUI/CLI workflow and thread it to the owning module.
- If a function needs many optional parameters, introduce a small settings
  dataclass owned by that feature.
- If a branch exists only for removed behavior, delete it with a targeted test
  update.

LOC and responsibility:

- Smaller is better when it clarifies ownership. Do not split files by arbitrary
  line count alone.
- Treat high `scc` complexity as a smell. First extract pure helpers, then
  remove unreachable branches, then consider splitting modules.
- Prefer one module with a clear public function over a chain of tiny wrappers
  that hide the real behavior.
- Keep tests close to behavior: one focused regression for each deleted path or
  changed contract.

## Simplification Rules

- Prefer deleting compatibility surfaces over preserving historical imports.
- Prefer direct module ownership over convenience barrels.
- Prefer one clear user path over several partial paths.
- Prefer small focused tests over broad refactors without assertions.
- Leave unrelated scratch trees such as `reverse/`, `third_party/`, and
  `nvuv-play/` alone unless cleanup is explicitly requested.
- If a cleanup changes behavior, document it as a product decision rather than
  hiding it inside a refactor.
