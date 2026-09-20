#!/usr/bin/env bash

set -euo pipefail

# Sourceable for the container harness and tests. Only the disposable build
# container's mirrorlists and sync cache are changed, never the host's.
refresh_cachyos_databases() {
    local mirror_dir="${1:-/etc/pacman.d}"
    local sync_dir="${2:-/var/lib/pacman/sync}"
    local mirror v3_mirror candidate attempt=0
    local -a mirrors v3_mirrors
    mapfile -t mirrors < <(sed -n 's/^Server *= *\(https:\/\/.*\)/\1/p' "$mirror_dir/cachyos-mirrorlist")
    mapfile -t v3_mirrors < <(sed -n 's/^Server *= *\(https:\/\/.*\)/\1/p' "$mirror_dir/cachyos-v3-mirrorlist")

    for mirror in "${mirrors[@]}"; do
        v3_mirror="${mirror/\$arch/\$arch_v3}"
        # Use only mirrors configured by the image for BOTH architectures.
        # Keep each database and its signature on the same server.
        if ! printf '%s\n' "${v3_mirrors[@]}" | grep -Fxq "$v3_mirror"; then
            continue
        fi
        attempt=$((attempt + 1))
        printf 'Server = %s\n' "$mirror" > "$mirror_dir/cachyos-mirrorlist"
        printf 'Server = %s\n' "$v3_mirror" > "$mirror_dir/cachyos-v3-mirrorlist"
        # A failed refresh must not leave a database/signature from a different
        # mirror behind. Other distribution databases remain untouched.
        rm -f "$sync_dir"/cachyos*.db "$sync_dir"/cachyos*.db.sig "$sync_dir"/cachyos*.db.part
        echo "==> signed CachyOS refresh, mirror $attempt/6: $mirror"
        if timeout 120 pacman -Syy --noconfirm; then
            # Keep the verified database, but allow package downloads to fall
            # back if this mirror has not synced a referenced package yet.
            # The caller uses -Su/-S, never another database refresh, and
            # pacman still verifies each downloaded package's signature.
            {
                printf 'Server = %s\n' "$mirror"
                for candidate in "${mirrors[@]}"; do
                    [[ "$candidate" == "$mirror" ]] || printf 'Server = %s\n' "$candidate"
                done
            } > "$mirror_dir/cachyos-mirrorlist"
            {
                printf 'Server = %s\n' "$v3_mirror"
                for candidate in "${v3_mirrors[@]}"; do
                    [[ "$candidate" == "$v3_mirror" ]] || printf 'Server = %s\n' "$candidate"
                done
            } > "$mirror_dir/cachyos-v3-mirrorlist"
            return 0
        fi
        if [[ "$attempt" -ge 6 ]]; then
            break
        fi
        echo "CachyOS refresh failed; trying another configured mirror" >&2
    done
    echo "CachyOS infrastructure failure: no verified database after $attempt mirrors; package build not run" >&2
    return 1
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    refresh_cachyos_databases "$@"
fi
