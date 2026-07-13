# PenguinBurner Flatpak

PenguinBurner publishes a self-hosted Flatpak repository at
`https://jpietek.github.io/PenguinBurner/penguin-burner.flatpakrepo`.

## Install

```bash
flatpak remote-add --user --if-not-exists penguinburner https://jpietek.github.io/PenguinBurner/penguin-burner.flatpakrepo
flatpak install --user -y penguinburner io.github.jpietek.PenguinBurner
flatpak run io.github.jpietek.PenguinBurner
```

Or as a single pasteable command:

```bash
flatpak remote-add --user --if-not-exists penguinburner https://jpietek.github.io/PenguinBurner/penguin-burner.flatpakrepo && flatpak install --user -y penguinburner io.github.jpietek.PenguinBurner && flatpak run io.github.jpietek.PenguinBurner
```

That single command installs and opens the Flatpak. Before its window appears,
PenguinBurner silently repairs and verifies the complete host integration. After that,
`penguin-burner`, `pburn`, `penguin-burner-ui`, `pburn-ui`,
`penguin-burner-cli`, `pburn-cli`, and `PENGUIN_BURNER` work from your `PATH`
just like the native packages.

## Host Wrappers

The first GUI launch adds these commands under `~/.local/bin`, forwarding each
one into the Flatpak sandbox:

- `penguin-burner`
- `pburn`
- `penguin-burner-ui`
- `pburn-ui`
- `penguin-burner-cli`
- `pburn-cli`
- `PENGUIN_BURNER`

Startup also registers the native Vulkan overlay layer and verifies the shipped
NVAPI shim; existing native/PyPI commands are left untouched. For manual repair,
run `flatpak run --user --command=penguin-burner-install-wrappers
io.github.jpietek.PenguinBurner`; add `--force` only to replace non-managed
commands deliberately.

If the commands are not found after installation, make sure `~/.local/bin` is on
your `PATH`.

## Update

Existing Flatpak users should update and launch once; startup refreshes the
complete integration automatically:

```bash
flatpak update --user -y io.github.jpietek.PenguinBurner && flatpak run io.github.jpietek.PenguinBurner
```

## Uninstall

To uninstall the Flatpak cleanly, remove the host wrappers before removing the
app. Cleanup removes only files created by PenguinBurner: the `~/.local/bin`
commands, user Vulkan manifest, and NVAPI shim fronts in Proton prefixes (the
real DLLs are restored). It does not remove config or Auto-UV profiles under
`~/.config/PenguinBurner`, so those stay available to PyPI/native installs.

```bash
flatpak run --user --command=penguin-burner-install-wrappers io.github.jpietek.PenguinBurner --uninstall
flatpak uninstall --user --delete-data io.github.jpietek.PenguinBurner
flatpak remote-delete --user penguinburner
```

## Launch

Launch the installed app at any time with:

```bash
flatpak run io.github.jpietek.PenguinBurner
```
