# PenguinBurner 0.4.5

## Bug fixes

- Overlay: fix phantom frame-generation detection. Titles without a Reflex
  marker stream (e.g. a simple game going from a 30fps menu to 120fps gameplay)
  no longer get the genuine framerate increase mis-read as 4x frame generation.
  The displayed/base "deinterlace" now only applies when a concurrent marker
  stream is present, so the overlay reports the true present rate instead of a
  phantom base + "FG" rate.

## Packaging and metadata

- Package the Fedora overlay entry points.
- Fix the Ubuntu PPA debhelper build.
- Restore GPL-or-later package metadata and fix the GPL license badge.
- Mark the project as Beta (Development Status :: 4 - Beta).

## Docs

- Mark power-limit control as planned and list the Steam import features.
