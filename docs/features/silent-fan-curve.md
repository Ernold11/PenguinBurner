# Silent Fan Curve

> Feature guide — see the [README](../../README.md) for the project overview.

After Auto-UV finds a stable undervolt, the GPU runs cooler — which makes a
quiet fan curve practical. PenguinBurner can generate one for you.

![Fan curve editor](../assets/fan-curve-editor.png)

## How it works

- Auto-UV writes a suggested quiet fan curve after final verification, stored in
  the PenguinBurner user config directory.
- It is **not applied by default**. Opt in at runtime:

  ```bash
  sudo ./penguin_burner.sh --daemonize --silent-fan-curve
  ```

  Or toggle **Silent fan curve** in the Profiles tab.

## Safety guard

Fan-curve generation is **blocked if the final load temperature is already too
hot** for a quiet curve. The safety target is `75°C` by default.

## Customizing

Reshape the generated curve by hand in the fan curve editor — see
[curve-editor.md](./curve-editor.md).
