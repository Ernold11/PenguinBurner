---
name: verify
description: How to drive PenguinBurner's Qt GUI and Steam integration for end-to-end verification on this host.
---

# Verifying PenguinBurner changes

## GUI panels (offscreen, real backends)

Panels are plain classes taking Qt modules + a manager; you can drive one
headless against the REAL manager (live Steam, daemon, NVML) without popping
a window:

```python
import os, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import sys; sys.path.insert(0, "/home/jp/PenguinBurner")
from ui.qt import import_qt
QtCore, QtGui, QtWidgets, _pg = import_qt()
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
from ui.components.steam_panel import SteamPanel
panel = SteamPanel(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets)  # real manager
def pump(s):
    end = time.time() + s
    while time.time() < end:
        app.processEvents(); time.sleep(0.05)
```

- `pump()` in a loop fires QTimers (the panels' poll timers work).
- Evidence: `panel.widget.resize(1080, 640); panel.widget.grab().save(".../shot.png")`.
  Offscreen renders the default light style — the app-wide dark stylesheet
  (`ui/styles.py`) is not applied; layout is representative, colors are not.
- Import Qt/`ui.qt` BEFORE `ui.components.*`.

## Steam on this host

- Steam runs logged-in with the CDP marker present; `SteamCdpClient()` connects
  for real (read probes like `app_launch_options`/`terminate_app_supported` are
  safe and read-only).
- A cheap, safe game for real launch/stop round-trips:
  **Coffee Talk Tokyo Demo, app id 3606110** (~400 MB, wrapped with
  PENGUIN_BURNER, overlay off). It launches in ~10-20 s and terminates cleanly.
- A running game session is visible as
  `pgrep -fa "SteamLaunch AppId=<id>"` (Steam's reaper carries it).
- Launching a wrapped game applies its per-game Auto-UV profile via the daemon
  and restores the standing profile on exit — normal operation on this box.
