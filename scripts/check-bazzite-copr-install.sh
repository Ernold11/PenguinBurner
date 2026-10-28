#!/usr/bin/env bash

# Install the published COPR package inside the Bazzite OCI image. Bazzite is
# Fedora Atomic: on a booted system users enable the COPR repo and layer the
# package with `rpm-ostree install penguin-burner` (plus a reboot). Layering
# resolves against the same repos and payload this container run exercises,
# so this proves the channel — repo metadata, dependency closure against
# Bazzite's preinstalled set, and the native artifacts in the shipped RPM —
# without needing a booted ostree system.
#
# Unlike check-fedora-package-build.sh this tests the *published* package,
# not the current tree, so it is a scheduled channel-health check rather
# than a pre-publish gate.

set -euo pipefail

ENGINE="${PENGUIN_BURNER_CONTAINER_ENGINE:-docker}"
NETWORK="${PENGUIN_BURNER_CONTAINER_NETWORK:-host}"
IMAGE="${PENGUIN_BURNER_BAZZITE_IMAGE:-ghcr.io/ublue-os/bazzite:stable}"
COPR="${PENGUIN_BURNER_COPR:-jpietek/penguin-burner}"

if ! command -v "$ENGINE" >/dev/null 2>&1; then
    echo "missing required command: $ENGINE" >&2
    exit 1
fi

echo "==> bazzite COPR install ($IMAGE, copr $COPR)"
"$ENGINE" run --rm --network "$NETWORK" -e COPR="$COPR" \
    "$IMAGE" bash -euo pipefail -c '
    source /etc/os-release
    echo "Bazzite base: Fedora $VERSION_ID"
    dnf5 -y copr enable "$COPR"
    dnf5 -y install penguin-burner
    rpm -ql penguin-burner > /tmp/package-contents.txt
    for artifact in /usr/libexec/penguin-burnerd \
        "overlay/nvapi_shim/nvapi64.dll" \
        "overlay/native_layer/libVkLayer_penguinburner_latency.so"; do
        if ! grep -q "$artifact" /tmp/package-contents.txt; then
            echo "package is missing $artifact" >&2
            exit 1
        fi
    done
    # The CLI must start against Bazzite'"'"'s preinstalled Python stack; this
    # catches dependency-closure gaps the file assertions cannot.
    penguin-burner-cli --help >/dev/null
    echo "bazzite install OK: $(rpm -q penguin-burner)"
'
