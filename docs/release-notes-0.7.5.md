<!-- Cut together with the pyproject.toml / burnerd Cargo.toml version bump. -->

# PenguinBurner 0.7.5

- Apply is session-only by default: the new "Apply on startup" toggle saves
  the applied profile for boot, and unticking it clears the saved boot
  profile immediately.
- Mobile GPUs: stock reset no longer fails on boards that reject the power
  limit setter, and profiles start on laptops without controllable fans.
- New recovery command: `penguin-burner-cli --restore-stock` resets the GPU
  to stock now and at boot, without the GUI, keeping saved profiles.
- Deleting the actively running profile restores stock instead of leaving
  its curve applied; a corrupted config can no longer break applies or get
  overwritten.
- Steam tab shows Proton vs Native Linux next to each game and grays the
  compatibility selector out for native games; the selector uses Steam's
  live runtime details.
- Install guidance for externally-managed distros (pipx on Fedora, Ubuntu,
  Debian) and a documented recovery path in the install guide.
