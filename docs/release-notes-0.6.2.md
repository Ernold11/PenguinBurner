# PenguinBurner 0.6.2

## Highlights

- Carry the Auto-OC retry and ladder fixes into the public release.
- Retry a failed Auto-OC clock at higher voltage before climbing to more MHz.
- Keep Auto-OC ladder steps strictly moving upward by voltage on sparse VF tables.
- Let `PB_OVERLAY=1` force native overlay text on even when the saved UI config
  has overlay text disabled.
