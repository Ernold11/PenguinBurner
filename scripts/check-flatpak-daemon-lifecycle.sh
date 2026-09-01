#!/usr/bin/env bash

# Rehearse the Flatpak elevated daemon-install transaction end to end inside
# a disposable systemd container: the REAL transaction rendered from the
# current tree (runtime/support/flatpak_daemon_install.py), the REAL
# tree-built penguin-burnerd, real systemd, real socket. This covers what the
# build smoke cannot reach: enable/restart/socket-wait (the issue #19
# "daemon unreachable until app restart" class), the fail-closed peer-UID
# gate for both the root install probe and the desktop user, idempotence,
# install-over-install, legacy PenguinBurner.service migration, and the
# failure path's rollback restoring the previous daemon without noise.
#
# GPU hardware is mocked through the compiled-in PENGUIN_BURNERD_TEST_ seams;
# pkexec/polkit stays out of scope (the transaction runs as container root,
# standing in for the pkexec hop). SELinux relabeling is masked by mounting
# an empty directory over /sys/fs/selinux: hosts that mount selinuxfs would
# otherwise abort the transaction inside the minimal container, which lacks
# restorecon and a loaded policy.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# systemd-as-PID-1 containers are podman's native mode; docker needs manual
# /sbin/init wiring, so podman is the default here unlike the other checks.
ENGINE="${PENGUIN_BURNER_CONTAINER_ENGINE:-podman}"
NETWORK="${PENGUIN_BURNER_CONTAINER_NETWORK:-host}"
IMAGE_TAG="penguin-burner-daemon-lifecycle"
CONTAINER="penguin-burner-daemon-lifecycle-run"
DAEMON_BINARY_OVERRIDE="${PENGUIN_BURNER_DAEMON_BINARY:-}"

die() {
    echo "error: $*" >&2
    exit 1
}

command -v "$ENGINE" >/dev/null 2>&1 || die "missing required command: $ENGINE"

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/penguin-burner-daemon-lifecycle.XXXXXX")"
cleanup() {
    "$ENGINE" rm -f "$CONTAINER" >/dev/null 2>&1 || true
    rm -rf "$WORK_DIR"
}
trap cleanup EXIT

if [[ -n "$DAEMON_BINARY_OVERRIDE" ]]; then
    daemon_binary="$DAEMON_BINARY_OVERRIDE"
else
    echo "==> building penguin-burnerd from the tree"
    (cd "$ROOT/burnerd" && cargo build --release --locked)
    daemon_binary="$ROOT/burnerd/target/release/penguin-burnerd"
fi
test -f "$daemon_binary" || die "daemon binary not found: $daemon_binary"

mkdir -p "$WORK_DIR/payload" "$WORK_DIR/selinux-mask"
cp "$daemon_binary" "$WORK_DIR/payload/penguin-burnerd"

cat > "$WORK_DIR/Containerfile" <<'EOF'
FROM fedora:latest
RUN dnf -y install systemd python3 util-linux && dnf clean all
CMD ["/sbin/init"]
EOF

cat > "$WORK_DIR/inside.sh" <<'INNER'
#!/bin/bash
set -euo pipefail
export PYTHONPATH=/src PYTHONDONTWRITEBYTECODE=1

# Render the unit and both transaction variants exactly as the GUI's
# elevated flow does: invoked by the desktop user (SUDO_UID), targeting the
# canonical root-owned binary path, with the mock-GPU seam armed because the
# container has no NVIDIA hardware.
SUDO_UID=1000 python3 - <<'PY'
from pathlib import Path

from runtime.support.flatpak_daemon_install import (
    build_flatpak_daemon_install_script,
)
from runtime.support.runtime_service import (
    DAEMON_BINARY,
    build_daemon_api_service_unit,
)

unit = build_daemon_api_service_unit("/src/penguin_burner.py", binary_path=DAEMON_BINARY)
unit = unit.replace(
    "[Service]\n",
    "[Service]\nEnvironment=PENGUIN_BURNERD_TEST_MOCK_GPU=1\n",
    1,
)
Path("/tmp/unit").write_text(unit, encoding="utf-8")
for action in ("none", "migrate-legacy"):
    Path(f"/tmp/install-{action}.sh").write_text(
        build_flatpak_daemon_install_script(runtime_action=action),
        encoding="utf-8",
    )
PY

PENGUIN_BURNER_SYSTEMD_UNIT_B64="$(base64 -w0 /tmp/unit)"
export PENGUIN_BURNER_SYSTEMD_UNIT_B64
export PENGUIN_BURNER_DAEMON_BINARY_SRC=/payload/penguin-burnerd
export PENGUIN_BURNER_RUNTIME_PYTHONPATH=/src
export PENGUIN_BURNER_RUNTIME_HOME=/root

echo "==> install transaction (fresh system)"
/bin/sh -eu -c "$(cat /tmp/install-none.sh)" penguin-burner-daemon-install
systemctl is-active penguin-burnerd.service
test -S /run/penguin-burnerd.sock

echo "==> wire status: root (install-time probe) and uid 1000 (gated user)"
python3 -m runtime.daemon_client status >/dev/null
setpriv --reuid 1000 --regid 1000 --clear-groups \
    env PYTHONPATH=/src PYTHONDONTWRITEBYTECODE=1 \
    python3 -m runtime.daemon_client status >/dev/null

echo "==> idempotent rerun over the existing install"
/bin/sh -eu -c "$(cat /tmp/install-none.sh)" penguin-burner-daemon-install >/dev/null
systemctl is-active penguin-burnerd.service

echo "==> legacy PenguinBurner.service migration"
cat > /etc/systemd/system/PenguinBurner.service <<'UNIT'
[Unit]
Description=legacy PenguinBurner service
[Service]
ExecStart=/bin/sleep infinity
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now PenguinBurner.service
/bin/sh -eu -c "$(cat /tmp/install-migrate-legacy.sh)" penguin-burner-daemon-install
if [ -e /etc/systemd/system/PenguinBurner.service ]; then
    echo "legacy unit survived migration" >&2
    exit 1
fi
systemctl is-active penguin-burnerd.service

echo "==> failure path: broken daemon rolls back to the previous install"
cp /usr/bin/true /tmp/broken-daemon
export PENGUIN_BURNER_DAEMON_BINARY_SRC=/tmp/broken-daemon
set +e
output="$(/bin/sh -eu -c "$(cat /tmp/install-none.sh)" penguin-burner-daemon-install 2>&1)"
status=$?
set -e
if [ "$status" -eq 0 ]; then
    echo "install of a broken daemon unexpectedly succeeded" >&2
    exit 1
fi
echo "$output" | grep -q "Restored the previous PenguinBurner hardware service" || {
    echo "rollback did not report restoring the previous service:" >&2
    echo "$output" >&2
    exit 1
}
if echo "$output" | grep -q "also failed"; then
    echo "rollback falsely reported failure:" >&2
    echo "$output" >&2
    exit 1
fi
systemctl is-active penguin-burnerd.service
/var/opt/penguin-burner/libexec/penguin-burnerd --version

echo "daemon lifecycle rehearsal OK"
INNER
chmod +x "$WORK_DIR/inside.sh"

echo "==> building systemd container image"
"$ENGINE" build -q -t "$IMAGE_TAG" -f "$WORK_DIR/Containerfile" "$WORK_DIR" >/dev/null

echo "==> starting systemd container"
"$ENGINE" rm -f "$CONTAINER" >/dev/null 2>&1 || true
"$ENGINE" run -d --name "$CONTAINER" --systemd=always --privileged \
    --network "$NETWORK" \
    -v "$WORK_DIR/selinux-mask:/sys/fs/selinux:ro" \
    -v "$ROOT:/src:ro" \
    -v "$WORK_DIR/payload:/payload:ro" \
    -v "$WORK_DIR/inside.sh:/inside.sh:ro" \
    "$IMAGE_TAG" >/dev/null
"$ENGINE" exec "$CONTAINER" bash -c \
    'systemctl is-system-running --wait >/dev/null 2>&1 || true'

"$ENGINE" exec "$CONTAINER" /inside.sh
