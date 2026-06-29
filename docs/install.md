# Install PenguinBurner

Install the NVIDIA proprietary driver and CUDA first. PenguinBurner supports
RTX 30 (Ampere), RTX 40 (Ada), and RTX 50 (Blackwell) cards.

The driver must provide `libnvidia-ml.so.1` for NVML telemetry/GPU discovery,
`libnvidia-api.so.1` for NVIDIA V/F curve control, the Vulkan runtime for Q2RTX,
and `libcuda.so.1` for the CUDA companion test. PenguinBurner reads GPU identity
and VRAM through NVML directly; the `nvidia-smi` command is useful for manual
debugging but is not the GPU picker backend.

## pip (any distro)

```bash
python -m pip install --user --upgrade penguin-burner
```

## Flatpak

```bash
flatpak remote-add --user --if-not-exists penguinburner https://jpietek.github.io/PenguinBurner/penguin-burner.flatpakrepo
flatpak install --user -y penguinburner io.github.jpietek.PenguinBurner
curl -fsSL https://jpietek.github.io/PenguinBurner/install-flatpak-cli-wrappers.sh | bash
```

The wrapper installer adds `penguin-burner`, `pburn`, `penguin-burner-ui`,
`pburn-ui`, `penguin-burner-cli`, `pburn-cli`, and `PENGUIN_BURNER` under
`~/.local/bin`, forwarding each command into the Flatpak sandbox. It refuses to
overwrite an existing native or PyPI command unless you rerun it as:

```bash
curl -fsSL https://jpietek.github.io/PenguinBurner/install-flatpak-cli-wrappers.sh | bash -s -- --force
```

The direct Flatpak launcher also remains available:

```bash
flatpak run io.github.jpietek.PenguinBurner
```

## Fedora ([COPR](https://copr.fedorainfracloud.org/coprs/jpietek/penguin-burner/))

Fedora 42 / 43 / 44, with the NVIDIA driver from Fedora's repo or RPM Fusion:

```bash
sudo dnf install -y dnf-plugins-core
sudo dnf copr enable -y jpietek/penguin-burner
sudo dnf install -y penguin-burner
```

## Arch / CachyOS ([AUR](https://aur.archlinux.org/packages/penguin-burner))

```bash
paru -S penguin-burner   # or: yay -S penguin-burner
```

## Ubuntu ([PPA](https://launchpad.net/~jpietek/+archive/ubuntu/penguin-burner))

Ubuntu 25.10 / 26.04:

```bash
sudo add-apt-repository ppa:jpietek/penguin-burner
sudo apt update && sudo apt install penguin-burner
```

## Entry points

- GUI: `penguin-burner` (alias `pburn`)
- CLI: `penguin-burner-cli` (alias `pburn-cli`)

The pip and Flatpak-wrapper installs also add desktop-friendly command entries.
If the commands are not found, make sure `~/.local/bin` is on your `PATH`.

## Local wheel (from a checkout)

```bash
python -m pip install --user --no-index --no-deps --find-links dist --upgrade penguin-burner
```
