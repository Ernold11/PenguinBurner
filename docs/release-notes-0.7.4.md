<!-- Cut together with the pyproject.toml / burnerd Cargo.toml version bump. -->

# PenguinBurner 0.7.4

- In-game telemetry is deduplicated per wrapper session: the NVAPI shim and
  the Vulkan layer feed one meter, shim markers preferred, layer as fallback.
- The layer's marker fallback stays active when a deployed shim never streams
  (game-local nvapi64.dll titles, 32-bit prefixes).
- Frame-generation latency includes the pacing hold again on the shim path.
- Removed superseded paths: the dxvk-nvapi trace fallback, the overlay-text
  file output, the legacy flatpak shell installer, and the
  `penguin-burner-ui`/`pburn-ui` aliases (old host wrappers are cleaned up
  automatically).
