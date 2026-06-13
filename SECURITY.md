# Security Policy

## Reporting a vulnerability

Please report security issues privately via
[GitHub Security Advisories](https://github.com/jpietek/PenguinBurner/security/advisories/new)
rather than a public issue. If that is unavailable, open a minimal
[issue](https://github.com/jpietek/PenguinBurner/issues) asking for a private
contact and we will follow up.

## Hardware-safety note

PenguinBurner performs real GPU hardware operations: enabling persistence mode,
setting board power limits, writing core and memory V/F offsets, and taking over
fan control. A bad voltage point can hang the GPU, crash the driver, freeze the
display, or force a reboot.

Auto-UV records the voltage under test before each risky probe and marks unsafe
voltages after a crash, but you run hardware tuning at your own risk. Test
changes on hardware you can recover.
