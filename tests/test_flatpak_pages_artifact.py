from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest


_HELPER_PATH = Path("scripts/flatpak_pages_artifact.py")
_SPEC = importlib.util.spec_from_file_location("flatpak_pages_artifact", _HELPER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
artifact = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = artifact
_SPEC.loader.exec_module(artifact)


def _fingerprint(_key: Path) -> str:
    return artifact.EXPECTED_SIGNING_FINGERPRINT


def _valid_site(tmp_path: Path) -> Path:
    site = tmp_path / "site"
    repository_ref = (
        site / "repo/refs/heads/app/io.github.jpietek.PenguinBurner/x86_64/master"
    )
    repository_ref.parent.mkdir(parents=True)
    repository_ref.write_text("commit\n", encoding="ascii")
    (site / "repo/config").write_text("[core]\nmode=archive-z2\n", encoding="ascii")
    (site / "repo/summary").write_bytes(b"summary")
    (site / "repo/summary.sig").write_bytes(b"signature")
    (site / ".nojekyll").touch()
    (site / "index.html").write_text("<!doctype html>\n", encoding="utf-8")
    (site / "PenguinBurner.flatpak").write_bytes(b"bundle")
    (site / "penguin-burner-flatpak.gpg").write_bytes(b"public-key")
    (site / "penguin-burner.flatpakrepo").write_text(
        "[Flatpak Repo]\n"
        "Title=PenguinBurner\n"
        f"Url={artifact.EXPECTED_REPOSITORY_URL}\n",
        encoding="utf-8",
    )
    return site


def _write_archive(path: Path, members: list[tarfile.TarInfo]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for member in members:
            data = b"x" if member.isfile() else None
            if data is not None:
                member.size = len(data)
            archive.addfile(member, io.BytesIO(data) if data is not None else None)


def test_validate_release_tag_accepts_version_and_rejects_paths() -> None:
    assert artifact.validate_release_tag("v0.7.2") == "v0.7.2"
    for tag in ("", "../v1", "feature/v1", "-v1", "v1 *"):
        with pytest.raises(artifact.ArtifactError):
            artifact.validate_release_tag(tag)


def test_valid_site_packs_deterministically_and_extracts(tmp_path: Path) -> None:
    site = _valid_site(tmp_path)
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"

    first_checksum = artifact.create_archive(
        site, first, fingerprint_reader=_fingerprint
    )
    second_checksum = artifact.create_archive(
        site, second, fingerprint_reader=_fingerprint
    )

    assert first.read_bytes() == second.read_bytes()
    assert first_checksum.read_text(encoding="ascii").endswith("  first.tar.gz\n")
    assert second_checksum.read_text(encoding="ascii").endswith("  second.tar.gz\n")
    extracted = tmp_path / "extracted"
    artifact.extract_archive(
        first,
        extracted,
        checksum_path=first_checksum,
        fingerprint_reader=_fingerprint,
    )
    assert (extracted / "PenguinBurner.flatpak").read_bytes() == b"bundle"
    assert (extracted / "repo/summary").read_bytes() == b"summary"


@pytest.mark.parametrize(
    "missing",
    (
        "index.html",
        "PenguinBurner.flatpak",
        "penguin-burner.flatpakrepo",
        "repo/summary",
    ),
)
def test_validate_site_rejects_missing_required_files(
    tmp_path: Path, missing: str
) -> None:
    site = _valid_site(tmp_path)
    (site / missing).unlink()
    with pytest.raises(artifact.ArtifactError, match="missing required file"):
        artifact.validate_site(site, fingerprint_reader=_fingerprint)


def test_validate_site_rejects_empty_payload(tmp_path: Path) -> None:
    site = _valid_site(tmp_path)
    (site / "PenguinBurner.flatpak").write_bytes(b"")
    with pytest.raises(artifact.ArtifactError, match="is empty"):
        artifact.validate_site(site, fingerprint_reader=_fingerprint)


def test_validate_site_rejects_repository_url_mismatch(tmp_path: Path) -> None:
    site = _valid_site(tmp_path)
    (site / "penguin-burner.flatpakrepo").write_text(
        "[Flatpak Repo]\nUrl=https://example.invalid/repo\n", encoding="utf-8"
    )
    with pytest.raises(artifact.ArtifactError, match="URL mismatch"):
        artifact.validate_site(site, fingerprint_reader=_fingerprint)


def test_validate_site_rejects_signing_key_mismatch(tmp_path: Path) -> None:
    site = _valid_site(tmp_path)
    with pytest.raises(artifact.ArtifactError, match="signing key mismatch"):
        artifact.validate_site(site, fingerprint_reader=lambda _path: "BAD")


def test_validate_site_rejects_symlink(tmp_path: Path) -> None:
    site = _valid_site(tmp_path)
    (site / "link").symlink_to("repo/summary")
    with pytest.raises(artifact.ArtifactError, match="symlinks"):
        artifact.validate_site(site, fingerprint_reader=_fingerprint)


@pytest.mark.parametrize(
    ("name", "entry_type"),
    (
        ("../escape", tarfile.REGTYPE),
        ("/absolute", tarfile.REGTYPE),
        ("./relative", tarfile.REGTYPE),
        ("nested//empty", tarfile.REGTYPE),
        ("link", tarfile.SYMTYPE),
        ("hard-link", tarfile.LNKTYPE),
    ),
)
def test_extract_rejects_unsafe_archive_entries(
    tmp_path: Path, name: str, entry_type: bytes
) -> None:
    archive_path = tmp_path / "unsafe.tar.gz"
    member = tarfile.TarInfo(name)
    member.type = entry_type
    if entry_type in (tarfile.SYMTYPE, tarfile.LNKTYPE):
        member.linkname = "target"
    _write_archive(archive_path, [member])
    checksum = artifact.write_checksum(archive_path)

    with pytest.raises(artifact.ArtifactError):
        artifact.extract_archive(
            archive_path,
            tmp_path / "output",
            checksum_path=checksum,
            fingerprint_reader=_fingerprint,
        )
    assert not (tmp_path / "output").exists()
    assert not (tmp_path.parent / "escape").exists()


def test_extract_rejects_wrapping_directory(tmp_path: Path) -> None:
    archive_path = tmp_path / "wrapped.tar.gz"
    member = tarfile.TarInfo("wrapper/index.html")
    member.type = tarfile.REGTYPE
    _write_archive(archive_path, [member])
    checksum = artifact.write_checksum(archive_path)

    with pytest.raises(artifact.ArtifactError, match="missing required file"):
        artifact.extract_archive(
            archive_path,
            tmp_path / "output",
            checksum_path=checksum,
            fingerprint_reader=_fingerprint,
        )


def test_extract_rejects_checksum_mismatch(tmp_path: Path) -> None:
    site = _valid_site(tmp_path)
    archive_path = tmp_path / "site.tar.gz"
    checksum = artifact.create_archive(
        site, archive_path, fingerprint_reader=_fingerprint
    )
    archive_path.write_bytes(archive_path.read_bytes() + b"corrupt")

    with pytest.raises(artifact.ArtifactError, match="checksum mismatch"):
        artifact.extract_archive(
            archive_path,
            tmp_path / "output",
            checksum_path=checksum,
            fingerprint_reader=_fingerprint,
        )


def test_public_key_fingerprint_reads_gpg_colon_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = tmp_path / "key.gpg"
    key.write_bytes(b"key")
    completed = subprocess.CompletedProcess(
        args=["gpg"],
        returncode=0,
        stdout=f"fpr:::::::::{artifact.EXPECTED_SIGNING_FINGERPRINT}:\n",
        stderr="",
    )
    monkeypatch.setattr(artifact.subprocess, "run", lambda *args, **kwargs: completed)

    assert artifact.public_key_fingerprint(key) == artifact.EXPECTED_SIGNING_FINGERPRINT
