# PenguinBurner 0.6.6

- Keep Auto-UV available on RTX 50-series mobile GPUs by disabling only fixed manual power-limit controls.
- Skip unsupported fixed power-limit getter/probe/write paths on identified laptop GPUs.
- Report NVML GPU-listing failures directly instead of mislabeling NVML init failures as missing GPUs.
