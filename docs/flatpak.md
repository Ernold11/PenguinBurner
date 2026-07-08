# PenguinBurner Flatpak

PenguinBurner publishes a self-hosted Flatpak repository at
`https://jpietek.github.io/PenguinBurner/penguin-burner.flatpakrepo`.

## Install

```bash
flatpak remote-add --user --if-not-exists penguinburner https://jpietek.github.io/PenguinBurner/penguin-burner.flatpakrepo
flatpak install --user -y penguinburner io.github.jpietek.PenguinBurner
flatpak run --user --command=penguin-burner-install-wrappers io.github.jpietek.PenguinBurner
```

Or as a single pasteable command:

```bash
flatpak remote-add --user --if-not-exists penguinburner https://jpietek.github.io/PenguinBurner/penguin-burner.flatpakrepo && flatpak install --user -y penguinburner io.github.jpietek.PenguinBurner && flatpak run --user --command=penguin-burner-install-wrappers io.github.jpietek.PenguinBurner
```

That single command installs the Flatpak and host command wrappers. After that,
`penguin-burner`, `pburn`, `penguin-burner-ui`, `pburn-ui`,
`penguin-burner-cli`, `pburn-cli`, and `PENGUIN_BURNER` work from your `PATH`
just like the native packages.

## Host Wrappers

The wrapper installer adds these commands under `~/.local/bin`, forwarding each
one into the Flatpak sandbox:

- `penguin-burner`
- `pburn`
- `penguin-burner-ui`
- `pburn-ui`
- `penguin-burner-cli`
- `pburn-cli`
- `PENGUIN_BURNER`

The wrapper installer also registers the native Vulkan overlay layer for your
user account, so Steam launch options can stay short. It refuses to overwrite
existing native/PyPI commands unless rerun with `--force`.

If the commands are not found after installation, make sure `~/.local/bin` is on
your `PATH`.

## Update

Existing Flatpak users should update and refresh the `PATH` wrappers with:

```bash
flatpak update --user -y io.github.jpietek.PenguinBurner && flatpak run --user --command=penguin-burner-install-wrappers io.github.jpietek.PenguinBurner
```

## Uninstall

To uninstall the Flatpak cleanly, remove the host wrappers before removing the
app. The wrapper cleanup removes only files created by the wrapper installer,
including the `~/.local/bin` commands and the user Vulkan layer manifest. It
does not remove your regular PenguinBurner config or Auto-UV profiles under
`~/.config/PenguinBurner`, so those stay available to PyPI/native installs.

```bash
flatpak run --user --command=penguin-burner-install-wrappers io.github.jpietek.PenguinBurner --uninstall
flatpak uninstall --user --delete-data io.github.jpietek.PenguinBurner
flatpak remote-delete --user penguinburner
```

## Direct Launch

You can also launch the Flatpak directly without wrappers:

```bash
flatpak run io.github.jpietek.PenguinBurner
```
