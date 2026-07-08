# PenguinBurner 0.6.4

- Repair Flatpak hardware service startup so profile apply waits for the daemon socket before reporting success.
- Prevent stale daemon runtime paths after clean reinstalls, moved installs, or service migration by clearing and rebasing persisted root runtime state.
