#!/usr/bin/env python3
"""Create and validate safe, deterministic PenguinBurner Pages snapshots."""

from __future__ import annotations

import argparse
import configparser
import gzip
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable, Iterable


APP_ID = "io.github.jpietek.PenguinBurner"
EXPECTED_REPOSITORY_URL = "https://jpietek.github.io/PenguinBurner/repo"
EXPECTED_SIGNING_FINGERPRINT = "BA1929DBD4CE4A49A8106D122800D243DB4657B3"
MAX_ARCHIVE_ENTRIES = 100_000
MAX_UNPACKED_SIZE = 2 * 1024 * 1024 * 1024
RELEASE_TAG_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")

REQUIRED_PATHS = (
    Path(".nojekyll"),
    Path("index.html"),
    Path("penguin-burner.flatpakrepo"),
    Path("penguin-burner-flatpak.gpg"),
    Path("PenguinBurner.flatpak"),
    Path("repo/config"),
    Path("repo/summary"),
    Path("repo/summary.sig"),
    Path(f"repo/refs/heads/app/{APP_ID}/x86_64/master"),
)
NONEMPTY_PATHS = REQUIRED_PATHS[1:]


class ArtifactError(ValueError):
    """Raised when a Pages snapshot is unsafe or internally inconsistent."""


FingerprintReader = Callable[[Path], str]


def validate_release_tag(tag: str) -> str:
    """Return a release tag that is safe to use in filenames and shell arguments."""

    if not RELEASE_TAG_PATTERN.fullmatch(tag):
        raise ArtifactError(
            "release tag must start with an alphanumeric character and contain "
            "only letters, numbers, dots, underscores, or hyphens"
        )
    return tag


def public_key_fingerprint(key_path: Path) -> str:
    """Read the first fingerprint from an exported OpenPGP public key."""

    result = subprocess.run(
        (
            "gpg",
            "--batch",
            "--with-colons",
            "--import-options",
            "show-only",
            "--import",
            str(key_path),
        ),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "gpg could not read the public key"
        raise ArtifactError(detail)

    for line in result.stdout.splitlines():
        fields = line.split(":")
        if fields[0] == "fpr" and len(fields) > 9 and fields[9]:
            return fields[9].upper()
    raise ArtifactError("public signing key does not contain a fingerprint")


def _tree_entries(root: Path) -> list[tuple[Path, Path, os.stat_result]]:
    entries: list[tuple[Path, Path, os.stat_result]] = []
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        directory_names.sort()
        file_names.sort()
        current_path = Path(current)
        for name in (*directory_names, *file_names):
            path = current_path / name
            metadata = path.lstat()
            relative = path.relative_to(root)
            if stat.S_ISLNK(metadata.st_mode):
                raise ArtifactError(f"snapshot must not contain symlinks: {relative}")
            if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
                raise ArtifactError(f"unsupported snapshot entry: {relative}")
            entries.append((relative, path, metadata))
    entries.sort(key=lambda item: item[0].as_posix())
    return entries


def validate_site(
    site_root: Path,
    *,
    fingerprint_reader: FingerprintReader = public_key_fingerprint,
) -> None:
    """Validate the static site layout, repository URL, and signing identity."""

    if site_root.is_symlink():
        raise ArtifactError(f"snapshot root must not be a symlink: {site_root}")
    root = site_root.resolve()
    if not root.is_dir():
        raise ArtifactError(f"snapshot root is not a directory: {site_root}")

    _tree_entries(root)
    for relative in REQUIRED_PATHS:
        path = root / relative
        if not path.is_file():
            raise ArtifactError(f"snapshot is missing required file: {relative}")
    for relative in NONEMPTY_PATHS:
        if (root / relative).stat().st_size == 0:
            raise ArtifactError(f"required snapshot file is empty: {relative}")

    remote = configparser.ConfigParser(interpolation=None)
    try:
        with (root / "penguin-burner.flatpakrepo").open(
            "r", encoding="utf-8"
        ) as stream:
            remote.read_file(stream)
        repository_url = remote["Flatpak Repo"]["Url"]
    except (configparser.Error, KeyError, UnicodeError) as exc:
        raise ArtifactError(f"invalid penguin-burner.flatpakrepo: {exc}") from exc
    if repository_url != EXPECTED_REPOSITORY_URL:
        raise ArtifactError(
            "Flatpak repository URL mismatch: "
            f"expected {EXPECTED_REPOSITORY_URL}, got {repository_url}"
        )

    fingerprint = fingerprint_reader(root / "penguin-burner-flatpak.gpg").upper()
    if fingerprint != EXPECTED_SIGNING_FINGERPRINT:
        raise ArtifactError(
            "Flatpak signing key mismatch: "
            f"expected {EXPECTED_SIGNING_FINGERPRINT}, got {fingerprint}"
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _default_checksum_path(archive_path: Path) -> Path:
    return Path(f"{archive_path}.sha256")


def write_checksum(archive_path: Path, checksum_path: Path | None = None) -> Path:
    """Write a sha256sum-compatible checksum file for an archive."""

    target = checksum_path or _default_checksum_path(archive_path)
    target.write_text(
        f"{sha256_file(archive_path)}  {archive_path.name}\n", encoding="ascii"
    )
    return target


def verify_checksum(archive_path: Path, checksum_path: Path) -> None:
    """Verify a checksum file and ensure that it names the supplied archive."""

    try:
        checksum_line = checksum_path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise ArtifactError(f"could not read checksum file: {exc}") from exc
    fields = checksum_line.split()
    if len(fields) != 2 or not re.fullmatch(r"[0-9a-fA-F]{64}", fields[0]):
        raise ArtifactError("checksum file must contain one SHA-256 and filename")
    recorded_name = fields[1].lstrip("*")
    if recorded_name != archive_path.name:
        raise ArtifactError(
            f"checksum names {recorded_name}, expected {archive_path.name}"
        )
    actual = sha256_file(archive_path)
    if actual.lower() != fields[0].lower():
        raise ArtifactError("snapshot archive checksum mismatch")


def create_archive(
    site_root: Path,
    archive_path: Path,
    *,
    checksum_path: Path | None = None,
    fingerprint_reader: FingerprintReader = public_key_fingerprint,
) -> Path:
    """Create a deterministic tar.gz snapshot and return its checksum path."""

    validate_site(site_root, fingerprint_reader=fingerprint_reader)
    root = site_root.resolve()
    output = archive_path.resolve()
    if output == root or root in output.parents:
        raise ArtifactError("snapshot archive must be written outside the site root")
    output.parent.mkdir(parents=True, exist_ok=True)
    entries = _tree_entries(root)

    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as raw_stream:
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=raw_stream, mtime=0
            ) as gzip_stream:
                with tarfile.open(
                    fileobj=gzip_stream, mode="w", format=tarfile.PAX_FORMAT
                ) as archive:
                    for relative, path, metadata in entries:
                        name = relative.as_posix()
                        info = tarfile.TarInfo(
                            f"{name}/" if stat.S_ISDIR(metadata.st_mode) else name
                        )
                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""
                        info.mtime = 0
                        if stat.S_ISDIR(metadata.st_mode):
                            info.type = tarfile.DIRTYPE
                            info.mode = 0o755
                            archive.addfile(info)
                        else:
                            info.type = tarfile.REGTYPE
                            info.mode = 0o644
                            info.size = metadata.st_size
                            with path.open("rb") as source:
                                archive.addfile(info, source)
            raw_stream.flush()
            os.fsync(raw_stream.fileno())
        os.replace(temporary_path, output)
        output.chmod(0o644)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise

    return write_checksum(output, checksum_path)


def _safe_member_path(name: str) -> PurePosixPath:
    raw_parts = name.split("/")
    if (
        not name
        or "\\" in name
        or name.startswith("/")
        or any(part in ("", ".", "..") for part in raw_parts)
    ):
        raise ArtifactError(f"unsafe archive path: {name!r}")
    path = PurePosixPath(name)
    return path


def _validated_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    if len(members) > MAX_ARCHIVE_ENTRIES:
        raise ArtifactError(f"snapshot archive exceeds {MAX_ARCHIVE_ENTRIES} entries")

    paths: dict[PurePosixPath, tarfile.TarInfo] = {}
    total_size = 0
    for member in members:
        path = _safe_member_path(member.name)
        if not (member.isdir() or member.isfile()):
            raise ArtifactError(f"unsupported archive entry type: {member.name}")
        if path in paths:
            raise ArtifactError(f"duplicate archive path: {member.name}")
        paths[path] = member
        if member.isfile():
            if member.size < 0:
                raise ArtifactError(f"archive file has a negative size: {member.name}")
            total_size += member.size
            if total_size > MAX_UNPACKED_SIZE:
                raise ArtifactError(
                    f"snapshot archive exceeds {MAX_UNPACKED_SIZE} unpacked bytes"
                )

    for path, member in paths.items():
        for parent in path.parents:
            if parent == PurePosixPath("."):
                break
            parent_member = paths.get(parent)
            if parent_member is not None and not parent_member.isdir():
                raise ArtifactError(
                    f"archive path has a non-directory parent: {member.name}"
                )
    return members


def _extract_members(
    archive: tarfile.TarFile, members: Iterable[tarfile.TarInfo], destination: Path
) -> None:
    ordered = sorted(members, key=lambda item: (not item.isdir(), item.name))
    for member in ordered:
        relative = _safe_member_path(member.name)
        output = destination.joinpath(*relative.parts)
        if member.isdir():
            output.mkdir(parents=True, exist_ok=True)
            output.chmod(0o755)
            continue
        output.parent.mkdir(parents=True, exist_ok=True)
        source = archive.extractfile(member)
        if source is None:
            raise ArtifactError(f"could not read archive file: {member.name}")
        with source, output.open("xb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
        if output.stat().st_size != member.size:
            raise ArtifactError(f"archive file size mismatch: {member.name}")
        output.chmod(0o644)


def extract_archive(
    archive_path: Path,
    destination: Path,
    *,
    checksum_path: Path,
    fingerprint_reader: FingerprintReader = public_key_fingerprint,
) -> None:
    """Safely extract and validate a Pages snapshot into a new directory."""

    if destination.exists() or destination.is_symlink():
        raise ArtifactError(f"extraction destination already exists: {destination}")
    verify_checksum(archive_path, checksum_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
    )
    try:
        try:
            with tarfile.open(archive_path, mode="r:gz") as archive:
                members = _validated_members(archive)
                _extract_members(archive, members, temporary)
        except (tarfile.TarError, OSError) as exc:
            raise ArtifactError(f"could not extract snapshot archive: {exc}") from exc
        validate_site(temporary, fingerprint_reader=fingerprint_reader)
        os.replace(temporary, destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    check_tag = commands.add_parser("check-tag", help="validate a release tag")
    check_tag.add_argument("tag")

    fingerprint = commands.add_parser(
        "fingerprint", help="print an exported public key fingerprint"
    )
    fingerprint.add_argument("key", type=Path)

    validate = commands.add_parser("validate", help="validate a static site tree")
    validate.add_argument("site", type=Path)

    pack = commands.add_parser("pack", help="create a deterministic snapshot")
    pack.add_argument("site", type=Path)
    pack.add_argument("archive", type=Path)

    extract = commands.add_parser("extract", help="safely extract a snapshot")
    extract.add_argument("archive", type=Path)
    extract.add_argument("destination", type=Path)
    extract.add_argument("--checksum", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "check-tag":
            print(validate_release_tag(args.tag))
        elif args.command == "fingerprint":
            print(public_key_fingerprint(args.key))
        elif args.command == "validate":
            validate_site(args.site)
        elif args.command == "pack":
            print(create_archive(args.site, args.archive))
        elif args.command == "extract":
            extract_archive(args.archive, args.destination, checksum_path=args.checksum)
        else:  # pragma: no cover - argparse enforces a known command
            raise ArtifactError(f"unknown command: {args.command}")
    except (ArtifactError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
