<!-- Cut together with the pyproject.toml / burnerd Cargo.toml version bump. -->

# PenguinBurner 0.7.6

- Auto-UV setup now installs or updates the hardware service before the
  dialog opens: upgrades from the 0.6.x service era no longer show a generic
  "NVIDIA GPU" with no limits — one prompt sets the service up and the
  dialog lists your real GPU.
- When the service is unreachable, the dialog says so and how to fix it
  instead of "NVML read-only info unavailable".
- Migrating from the old PenguinBurner.service now also removes its unit
  file after the new service is verified running.
- Source RPMs are built from the committed tree only.
