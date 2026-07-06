# PenguinBurner 0.6

## Highlights

- Fixed the latency meter for some DX12 titles where marker handling could break,
  using the NVAPI DLL shim.
- Improved VRAM memory offset handling with proper ranges, units, and offset
  application.
- Improved power-limit handling across Auto-UV profiles.
- Added multiple fixes and tuning improvements for Efficiency, Balanced, and
  Performance profiles.
- Improved the root-owned daemon so it can apply low-level NVML/NVAPI operations
  through the daemon path.
