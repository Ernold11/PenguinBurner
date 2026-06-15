# PenguinBurner 0.4.7

## Fixes

- **Q2RTX verification crash on systems without OpenSSL 1.1** (e.g. Bazzite) —
  the bundled Q2RTX binary needs `libssl.so.1.1` / `libcrypto.so.1.1` but ships a
  non-relocatable RUNPATH (`/mnt/q2rtx/.`, NVIDIA's build path), so on distros
  that don't provide OpenSSL 1.1 it died mid-run with `error while loading shared
  libraries: libssl.so.1.1`, most visibly during the final verification pass when
  launched through gamescope. PenguinBurner now stages OpenSSL 1.1 next to the
  binary and rewrites its RUNPATH to `$ORIGIN`, so the libraries resolve from the
  binary's own directory on every launch — independent of the system libraries
  and of how the process is wrapped (a privilege-dropped, gamescope-wrapped
  `AT_SECURE` launch strips `LD_LIBRARY_PATH`).
- **Transient launcher failures no longer fail a run or blacklist a voltage** —
  a Q2RTX launch that dies before producing stable metrics (a flaky headless
  gamescope startup, or a loader error) is now retried in place a few times, and
  an unrecovered launch is reported as an environment error instead of being
  recorded as an unsafe undervolt.
