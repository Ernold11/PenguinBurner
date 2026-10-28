#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# The elevated Flatpak install step runs product code on the host python, so
# gate publication on the containerized host-python compatibility scenarios
# the same way the COPR and PPA publishes gate on their smoke builds. The
# full local build+install rehearsal below covers the sandbox side.
# PENGUIN_BURNER_SKIP_PACKAGE_SMOKE=1 skips the gate.
if [ "${PENGUIN_BURNER_SKIP_PACKAGE_SMOKE:-0}" != 1 ]; then
    "$ROOT/scripts/check-flatpak-host-python.sh"
fi

APP_ID="io.github.jpietek.PenguinBurner"
APP_ARCH="x86_64"
GITHUB_REPOSITORY="${PENGUIN_BURNER_GITHUB_REPOSITORY:-jpietek/PenguinBurner}"
PUBLIC_REPO_URL="https://jpietek.github.io/PenguinBurner/repo"
ARTIFACT_HELPER="$ROOT/scripts/flatpak_pages_artifact.py"
BUILD_SCRIPT="$ROOT/scripts/build-flatpak-public-repo.sh"
WORKFLOW_FILE="deploy-flatpak-pages.yml"
OUTPUT_DIR="${PENGUIN_BURNER_FLATPAK_PAGES_DIST:-$ROOT/dist/flatpak-pages}"
SMOKE_NO_DEPS="${PENGUIN_BURNER_FLATPAK_SMOKE_NO_DEPS:-0}"

replace=0
prepare_only=0
upload_only=0

usage() {
    cat <<'EOF'
Usage: scripts/publish-flatpak-pages.sh [--prepare-only|--upload-only] [--replace] TAG

Build, verify, and publish a signed Flatpak Pages snapshot without committing
generated files to Git.

Normal publication:
  - requires a clean checkout at TAG;
  - seeds the OSTree repository from the newest compatible Pages snapshot
    attached to an earlier GitHub Release;
  - builds and signs locally, uploads versioned Release assets, and dispatches
    the GitHub Pages deployment workflow.

Options:
  --prepare-only     Build, verify, and package without uploading or deploying.
  --upload-only      Build, verify, package, and upload without dispatching.
  --replace          Explicitly replace existing snapshot Release assets.
  -h, --help         Show this help.
EOF
}

die() {
    echo "error: $*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

snapshot_name() {
    printf 'PenguinBurner-pages-%s.tar.gz\n' "$1"
}

release_has_asset() {
    local release_tag="$1"
    local asset_name="$2"
    gh release view "$release_tag" \
        --repo "$GITHUB_REPOSITORY" \
        --json assets \
        --jq '.assets[].name' | grep -Fxq "$asset_name"
}

find_previous_snapshot_tag() {
    local target_tag="$1"
    local target_prerelease="$2"
    local target_published_at="$3"
    local candidate
    local candidate_archive
    local -a candidates=()

    mapfile -t candidates < <(
        gh release list \
            --repo "$GITHUB_REPOSITORY" \
            --limit 100 \
            --json tagName,isDraft,isPrerelease,publishedAt \
            --jq "sort_by(.publishedAt) | reverse | .[] | select(.isDraft == false and .isPrerelease == $target_prerelease and .publishedAt < \"$target_published_at\") | .tagName"
    )
    for candidate in "${candidates[@]}"; do
        [[ "$candidate" == "$target_tag" ]] && continue
        if ! python3 "$ARTIFACT_HELPER" check-tag "$candidate" >/dev/null 2>&1; then
            continue
        fi
        candidate_archive="$(snapshot_name "$candidate")"
        if release_has_asset "$candidate" "$candidate_archive" \
            && release_has_asset "$candidate" "$candidate_archive.sha256"; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

write_index() {
    local site="$1"
    rm -f "$site/repo/.lock"
    cat >"$site/index.html" <<'EOF'
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PenguinBurner Flatpak Repository</title>
</head>
<body>
  <h1>PenguinBurner Flatpak Repository</h1>
  <p><a href="penguin-burner.flatpakrepo">Add the Flatpak repository</a></p>
  <p><a href="PenguinBurner.flatpak">Download the current Flatpak bundle</a></p>
</body>
</html>
EOF
    : >"$site/.nojekyll"
}

flatpak_in_profile() {
    local profile="$1"
    shift
    env \
        XDG_DATA_HOME="$profile/data" \
        XDG_CONFIG_HOME="$profile/config" \
        XDG_CACHE_HOME="$profile/cache" \
        XDG_RUNTIME_DIR="$profile/runtime" \
        flatpak --user "$@"
}

prepare_flatpak_profile() {
    local profile="$1"
    mkdir -p \
        "$profile/data" \
        "$profile/config" \
        "$profile/cache" \
        "$profile/runtime"
    chmod 700 "$profile/runtime"
    flatpak_in_profile "$profile" remote-add \
        --if-not-exists \
        flathub \
        https://dl.flathub.org/repo/flathub.flatpakrepo
}

smoke_install() {
    local site="$1"
    local profile="$2"
    local remote_name="$3"
    local -a install_options=(install -y --arch="$APP_ARCH")
    if [[ "$SMOKE_NO_DEPS" == "1" ]]; then
        install_options+=(--no-deps)
    fi
    prepare_flatpak_profile "$profile"
    flatpak_in_profile "$profile" remote-add \
        --if-not-exists \
        --gpg-import="$site/penguin-burner-flatpak.gpg" \
        "$remote_name" \
        "file://$site/repo"
    flatpak_in_profile "$profile" "${install_options[@]}" "$remote_name" "$APP_ID"
    flatpak_in_profile "$profile" info --arch="$APP_ARCH" "$APP_ID" >/dev/null
}

smoke_update() {
    local previous_site="$1"
    local new_site="$2"
    local profile="$3"
    local -a update_options=(update -y --arch="$APP_ARCH")
    local old_commit
    local new_commit

    smoke_install "$previous_site" "$profile" penguinburner-test
    if [[ "$SMOKE_NO_DEPS" == "1" ]]; then
        update_options+=(--no-deps)
    fi
    old_commit="$(
        flatpak_in_profile "$profile" info \
            --arch="$APP_ARCH" \
            --show-commit \
            "$APP_ID"
    )"
    flatpak_in_profile "$profile" remote-modify \
        --url="file://$new_site/repo" \
        penguinburner-test
    flatpak_in_profile "$profile" "${update_options[@]}" "$APP_ID"
    new_commit="$(
        flatpak_in_profile "$profile" info \
            --arch="$APP_ARCH" \
            --show-commit \
            "$APP_ID"
    )"
    [[ -n "$new_commit" ]] || die "updated Flatpak does not report a commit"
    [[ "$new_commit" != "$old_commit" ]] \
        || die "new snapshot did not produce a new Flatpak commit"
}

while (($#)); do
    case "$1" in
        --replace)
            replace=1
            shift
            ;;
        --prepare-only)
            prepare_only=1
            shift
            ;;
        --upload-only)
            upload_only=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --*)
            die "unknown option: $1"
            ;;
        *)
            break
            ;;
    esac
done

(($# == 1)) || {
    usage >&2
    exit 2
}
((prepare_only == 0 || upload_only == 0)) \
    || die "--prepare-only and --upload-only are mutually exclusive"
tag="$1"

for command in git gh python3 gpg flatpak tar; do
    require_command "$command"
done
python3 "$ARTIFACT_HELPER" check-tag "$tag" >/dev/null
git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null
git -C "$ROOT" rev-parse --verify "$tag^{commit}" >/dev/null \
    || die "local release tag does not exist: $tag"
gh release view "$tag" --repo "$GITHUB_REPOSITORY" >/dev/null \
    || die "GitHub Release does not exist: $tag"

work_dir="$(mktemp -d "${TMPDIR:-/tmp}/penguin-burner-pages.XXXXXX")"
trap 'rm -rf "$work_dir"' EXIT
site="$work_dir/site"
previous_site="$work_dir/previous-site"
mkdir -p "$site"

[[ -z "$(git -C "$ROOT" status --porcelain --untracked-files=normal)" ]] \
    || die "normal publication requires a clean checkout"
head_commit="$(git -C "$ROOT" rev-parse HEAD)"
tag_commit="$(git -C "$ROOT" rev-parse "$tag^{commit}")"
[[ "$head_commit" == "$tag_commit" ]] \
    || die "normal publication requires HEAD to match $tag"

target_prerelease="$(
    gh release view "$tag" \
        --repo "$GITHUB_REPOSITORY" \
        --json isPrerelease \
        --jq '.isPrerelease'
)"
target_published_at="$(
    gh release view "$tag" \
        --repo "$GITHUB_REPOSITORY" \
        --json publishedAt \
        --jq '.publishedAt'
)"
previous_tag="$(
    find_previous_snapshot_tag \
        "$tag" \
        "$target_prerelease" \
        "$target_published_at"
)" \
    || die "no compatible earlier Pages snapshot found"
previous_archive="$(snapshot_name "$previous_tag")"
mkdir -p "$work_dir/download"
gh release download "$previous_tag" \
    --repo "$GITHUB_REPOSITORY" \
    --pattern "$previous_archive" \
    --pattern "$previous_archive.sha256" \
    --dir "$work_dir/download"
python3 "$ARTIFACT_HELPER" extract \
    "$work_dir/download/$previous_archive" \
    "$previous_site" \
    --checksum "$work_dir/download/$previous_archive.sha256"
cp -a "$previous_site/." "$site/"

PENGUIN_BURNER_FLATPAK_OUT_DIR="$site" \
PENGUIN_BURNER_FLATPAK_REPO_URL="$PUBLIC_REPO_URL" \
PENGUIN_BURNER_FLATPAK_BUILD_BUNDLE=1 \
    "$BUILD_SCRIPT"
write_index "$site"
python3 "$ARTIFACT_HELPER" validate "$site"
smoke_install "$site" "$work_dir/new-install" penguinburner-new
smoke_update "$previous_site" "$site" "$work_dir/update-install"

mkdir -p "$OUTPUT_DIR"
archive_name="$(snapshot_name "$tag")"
archive_path="$OUTPUT_DIR/$archive_name"
checksum_path="$archive_path.sha256"
python3 "$ARTIFACT_HELPER" pack "$site" "$archive_path" >/dev/null
[[ -f "$checksum_path" ]] || die "snapshot checksum was not created"

if ((prepare_only)); then
    cat <<EOF
Prepared and verified signed Pages snapshot for $tag without uploading:
  Archive:  $archive_path
  Checksum: $checksum_path
EOF
    exit 0
fi

if ((replace == 0)); then
    if release_has_asset "$tag" "$archive_name" \
        || release_has_asset "$tag" "$archive_name.sha256"; then
        die "snapshot assets already exist for $tag; pass --replace to replace them"
    fi
    upload_options=()
else
    upload_options=(--clobber)
fi

gh release upload "$tag" \
    --repo "$GITHUB_REPOSITORY" \
    "${upload_options[@]}" \
    "$archive_path" \
    "$checksum_path"

if ((upload_only)); then
    cat <<EOF
Uploaded signed Pages snapshot for $tag without dispatching:
  Release: https://github.com/$GITHUB_REPOSITORY/releases/tag/$tag

After changing Settings → Pages → Source to GitHub Actions, deploy with:
  gh workflow run $WORKFLOW_FILE --repo $GITHUB_REPOSITORY --field tag=$tag
EOF
    exit 0
fi

gh workflow run "$WORKFLOW_FILE" \
    --repo "$GITHUB_REPOSITORY" \
    --field "tag=$tag"

cat <<EOF
Uploaded signed Pages snapshot for $tag:
  Archive:  $archive_path
  Checksum: $checksum_path
  Release:  https://github.com/$GITHUB_REPOSITORY/releases/tag/$tag
  Runs:     https://github.com/$GITHUB_REPOSITORY/actions/workflows/$WORKFLOW_FILE
EOF
