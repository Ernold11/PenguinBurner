#!/usr/bin/env bash
# Launch PenguinBurner against a throwaway, empty home so you can test the
# first-run / new-user experience WITHOUT touching your real profiles, tiers,
# curves, or config.
#
# Everything the app reads or writes — config (~/.config/PenguinBurner),
# saved profiles, runtime config, data (~/.local/share), and cache
# (~/.cache) — is redirected under a sandbox directory via PENGUIN_BURNER_HOME
# and the XDG_* base-dir variables. Your real $HOME is never written to.
#
# Usage:
#   scripts/run-clean-profile.sh                 # fresh empty sandbox, launch GUI
#   scripts/run-clean-profile.sh --cli --help    # run the CLI instead of the GUI
#   PB_CLEAN_DIR=/tmp/pb-keep scripts/run-clean-profile.sh   # reuse a fixed sandbox
#   PB_CLEAN_BIN=penguin-burner-cli scripts/run-clean-profile.sh   # pick the entry point
#
# The sandbox path is printed on launch. It is NOT auto-deleted so you can
# inspect what a clean run created; remove it with: rm -rf <printed path>
set -euo pipefail

# --cli is a convenience alias for running the CLI entry point.
DEFAULT_BIN="penguin-burner"
if [[ "${1:-}" == "--cli" ]]; then
  shift
  DEFAULT_BIN="penguin-burner-cli"
fi

# Resolve the launcher binary on the *real* PATH before we rewrite the env.
BIN="${PB_CLEAN_BIN:-$DEFAULT_BIN}"
BIN_PATH="$(command -v "$BIN" || true)"
if [[ -z "$BIN_PATH" ]]; then
  echo "error: '$BIN' not found on PATH (is penguin-burner installed?)" >&2
  exit 1
fi

# A fresh empty sandbox each run, unless PB_CLEAN_DIR pins one.
SANDBOX="${PB_CLEAN_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/penguin-burner-clean.XXXXXX")}"
mkdir -p \
  "$SANDBOX/.config" "$SANDBOX/.local/share" "$SANDBOX/.cache" "$SANDBOX/run"
chmod 700 "$SANDBOX/run"

echo "PenguinBurner clean run"
echo "  binary : $BIN_PATH"
echo "  sandbox: $SANDBOX   (your real \$HOME is untouched)"
echo "  remove : rm -rf '$SANDBOX'"
echo

exec env \
  HOME="$SANDBOX" \
  PENGUIN_BURNER_HOME="$SANDBOX" \
  XDG_CONFIG_HOME="$SANDBOX/.config" \
  XDG_DATA_HOME="$SANDBOX/.local/share" \
  XDG_CACHE_HOME="$SANDBOX/.cache" \
  XDG_RUNTIME_DIR="$SANDBOX/run" \
  "$BIN_PATH" "$@"
