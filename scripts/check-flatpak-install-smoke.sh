#!/usr/bin/env bash

set -euo pipefail

APP_ID="io.github.jpietek.PenguinBurner"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="$ROOT/packaging/flatpak/$APP_ID.yml"
BUILD_DIR="${PENGUIN_BURNER_FLATPAK_SMOKE_BUILD_DIR:-}"
STATE_DIR="${PENGUIN_BURNER_FLATPAK_SMOKE_STATE_DIR:-}"
WORK_DIR="${PENGUIN_BURNER_FLATPAK_SMOKE_WORK_DIR:-}"
RUNTIME_BRANCH="${PENGUIN_BURNER_FLATPAK_RUNTIME_BRANCH:-25.08}"
CONTAINER_IMAGE="${PENGUIN_BURNER_FLATPAK_SMOKE_IMAGE:-fedora:latest}"

usage() {
    cat <<EOF
Usage: $0 [--container|--host]

Build and install the PenguinBurner Flatpak in an isolated user profile, then
prove that:
  - native host commands such as penguin-burner and pburn do not leak in;
  - Flatpak exports the app-id launcher;
  - the packaged /app/bin entry points exist inside the sandbox;
  - the non-GUI CLI entry points run --help successfully.

Options:
  --host       run the smoke test directly on this host (default)
  --container run the same smoke test in a disposable Docker/Podman container
EOF
}

die() {
    echo "error: $*" >&2
    exit 1
}

cleanup_work_dir() {
    if [[ -z "${WORK_DIR:-}" || ! -d "$WORK_DIR" ]]; then
        return
    fi

    if command -v fusermount3 >/dev/null 2>&1; then
        find "$WORK_DIR" -mindepth 1 -maxdepth 4 -type d -name 'rofiles-*' \
            -exec fusermount3 -u {} \; >/dev/null 2>&1 || true
    fi
    find "$WORK_DIR" -mindepth 1 -maxdepth 4 -type d -name 'rofiles-*' \
        -exec umount -l {} \; >/dev/null 2>&1 || true
    rm -rf "$WORK_DIR" >/dev/null 2>&1 || true
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

container_engine() {
    if command -v docker >/dev/null 2>&1; then
        echo docker
    elif command -v podman >/dev/null 2>&1; then
        echo podman
    else
        die "missing container engine: install docker or podman"
    fi
}

run_container() {
    local engine
    engine="$(container_engine)"

    "$engine" run --rm --privileged --security-opt label=disable \
        -v "$ROOT:/src:ro" \
        -w /src \
        "$CONTAINER_IMAGE" \
        bash -c '
            set -euo pipefail
            dnf -q install -y flatpak flatpak-builder >/dev/null
            /src/scripts/check-flatpak-install-smoke.sh --host
        '
}

flatpak_ref_exists() {
    local ref="$1"
    flatpak info "$ref" >/dev/null 2>&1
}

ensure_runtime() {
    local runtime_ref="org.freedesktop.Platform/x86_64/$RUNTIME_BRANCH"
    local sdk_ref="org.freedesktop.Sdk/x86_64/$RUNTIME_BRANCH"

    if flatpak_ref_exists "$runtime_ref" && flatpak_ref_exists "$sdk_ref"; then
        return
    fi

    flatpak --user remote-add --if-not-exists \
        flathub https://dl.flathub.org/repo/flathub.flatpakrepo
    flatpak --user install -y flathub "$runtime_ref" "$sdk_ref"
}

assert_no_host_short_commands() {
    local path_copy="$PATH"
    export PATH="/usr/bin:/bin"

    for bin in penguin-burner pburn penguin-burner-ui pburn-ui penguin-burner-cli pburn-cli; do
        if command -v "$bin" >/dev/null 2>&1; then
            die "host command leaked into smoke test PATH: $bin"
        fi
    done

    export PATH="$path_copy"
}

assert_flatpak_exports() {
    local export_bin="$XDG_DATA_HOME/flatpak/exports/bin/$APP_ID"

    test -x "$export_bin" || die "Flatpak app-id launcher was not exported: $export_bin"

    if PATH="$XDG_DATA_HOME/flatpak/exports/bin:/usr/bin:/bin" command -v penguin-burner >/dev/null 2>&1; then
        die "Flatpak unexpectedly exported native-style penguin-burner host command"
    fi

    PATH="$XDG_DATA_HOME/flatpak/exports/bin:/usr/bin:/bin" command -v "$APP_ID" >/dev/null
}

assert_sandbox_entrypoints() {
    flatpak run --user --command=bash "$APP_ID" -c '
        set -euo pipefail
        for bin in penguin-burner pburn penguin-burner-ui pburn-ui penguin-burner-cli pburn-cli PENGUIN_BURNER; do
            test -x "/app/bin/$bin"
            resolved="$(command -v "$bin")"
            test "$resolved" = "/app/bin/$bin"
        done
    '

    flatpak run --user --command=penguin-burner-cli "$APP_ID" --help >/dev/null
    flatpak run --user --command=pburn-cli "$APP_ID" --help >/dev/null
}

run_host() {
    require_command flatpak
    require_command flatpak-builder

    test -f "$MANIFEST" || die "missing Flatpak manifest: $MANIFEST"

    if [[ -z "$WORK_DIR" ]]; then
        WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/penguin-burner-flatpak-smoke.XXXXXX")"
    else
        mkdir -p "$WORK_DIR"
    fi
    trap cleanup_work_dir EXIT

    mkdir -p "$WORK_DIR"/{home,xdg-data,xdg-config,xdg-cache,xdg-state}
    export HOME="$WORK_DIR/home"
    export XDG_DATA_HOME="$WORK_DIR/xdg-data"
    export XDG_CONFIG_HOME="$WORK_DIR/xdg-config"
    export XDG_CACHE_HOME="$WORK_DIR/xdg-cache"
    export XDG_STATE_HOME="$WORK_DIR/xdg-state"

    BUILD_DIR="${BUILD_DIR:-$WORK_DIR/build/$APP_ID}"
    STATE_DIR="${STATE_DIR:-$WORK_DIR/state}"

    assert_no_host_short_commands
    ensure_runtime

    flatpak-builder \
        --user \
        --force-clean \
        --state-dir="$STATE_DIR" \
        --install \
        "$BUILD_DIR" \
        "$MANIFEST"

    flatpak --user info "$APP_ID" >/dev/null
    assert_flatpak_exports
    assert_sandbox_entrypoints

    cat <<EOF
Flatpak smoke test passed.
  Isolated HOME: $HOME
  Exported launcher: $XDG_DATA_HOME/flatpak/exports/bin/$APP_ID
  GUI launch: flatpak run $APP_ID
  CLI launch: flatpak run --command=pburn-cli $APP_ID --help
EOF
}

mode="host"
while (($#)); do
    case "$1" in
        --container)
            mode="container"
            ;;
        --host)
            mode="host"
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            exit 2
            ;;
    esac
    shift
done

if [[ "$mode" == "container" ]]; then
    run_container
else
    run_host
fi
