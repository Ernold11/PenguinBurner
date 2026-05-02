# UI Cleanup Notes

The Qt UI now lives in the `ui/` package. The old package has been retired, so
new work should extend the modules below instead of adding another window-level
blob.

## Current Ownership

- `ui/main.py`: Qt bootstrap, argument cleanup, app metadata, and palette setup.
- `ui/window.py`: composition root that wires components, dialogs, controllers,
  and workflow callbacks together.
- `ui/styles.py` and `ui/theme.py`: shared stylesheet and color tokens.
- `ui/components/`: reusable widgets such as tables, plots, headers, controls,
  and curve editors.
- `ui/dialogs/`: modal decisions that return plain Python data to the window.
- `ui/controllers/`: long-running command/process handling.
- `ui/models.py`: Qt-free payload-to-display helpers.
- `ui/profiles.py`: profile lookup, systemd status parsing, and profile text.
- `ui/curve_profiles.py`: V/F curve payload extraction, cache, and save helpers.
- `ui/fan_profiles.py`: fan curve payload extraction, cache, and save helpers.
- `ui/afterburner_import.py`: Afterburner profile discovery, import, summary,
  and deletion helpers.
- `ui/lact_export.py`: LACT config discovery and export helpers.
- `ui/tuning.py`: Auto-UV tuning defaults and value mapping.
- `ui/verify.py`: profile verification progress and stop request helpers.

## Adding A Component

1. Put reusable widgets in `ui/components/`.
2. Give each component one public `widget` attribute for layout placement.
3. Keep process launch, filesystem writes, and app state out of components.
4. Expose user intent through callbacks or Qt signals.
5. Put modal flows in `ui/dialogs/` and return dicts, dataclasses, or `None`.
6. Keep shared formatting and payload parsing in Qt-free helper modules.
7. Add the component to `ui/window.py` by composition.
8. Add focused tests for formatting, rows, state, and callbacks.

The window should answer "which pieces are connected?" Components should answer
"what do I show?" and "what did the user click?"

## Comment Style

Use concise one-line comments where they explain ownership, lifecycle, process
contracts, or hardware-side effects.

```python
# Dialogs return plain data; the window owns state mutation.
options = select_scan_tuning(...)
```

```python
# QProcess output can arrive in partial chunks.
```

```python
# The runtime service reads this shared artifact when silent fan mode is used.
write_auto_uv_fan_curve_payload(payload)
```

Avoid comments that only repeat the next line.

```python
# Create a button.
self.start_button = QtWidgets.QPushButton("Start")
```
