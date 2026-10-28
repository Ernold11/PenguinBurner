<!-- Cut together with the pyproject.toml / burnerd Cargo.toml version bump. -->

# PenguinBurner 0.7.7

- Fixes RTX 5090-class and other power-limited Auto-UV scenarios, including
  measured power walls, hardware brake evidence, capped-baseline clock reclaim,
  and safe candidate reuse
  ([#36](https://github.com/jpietek/PenguinBurner/pull/36),
  [#37](https://github.com/jpietek/PenguinBurner/pull/37) by
  [@jpietek](https://github.com/jpietek)).
- Skips unsupported fixed power writes on mobile GPUs while preserving V/F and
  memory tuning ([#31](https://github.com/jpietek/PenguinBurner/pull/31) by
  [@MihneaTeodorStoica](https://github.com/MihneaTeodorStoica)).
- Improves saved-profile verification, adds memory-offset editing, and shows the
  curve with the live GPU position
  ([#32](https://github.com/jpietek/PenguinBurner/pull/32),
  [#33](https://github.com/jpietek/PenguinBurner/pull/33),
  [#34](https://github.com/jpietek/PenguinBurner/pull/34), and
  [#35](https://github.com/jpietek/PenguinBurner/pull/35) by
  [@nanomad](https://github.com/nanomad)).
- Keeps the silent fan curve selected across Auto-UV scans.
