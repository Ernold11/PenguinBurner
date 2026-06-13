from __future__ import annotations

import bz2
import gzip
import io
import lzma
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

from common.penguin_burner_paths import claim_desktop_user_ownership
from common.subprocess_locale import stable_subprocess_env

from .models import StabilityTestError


def _subprocess_c_locale_env() -> dict[str, str]:
    return stable_subprocess_env()


def _payload_starts_with_cpio_header(payload: bytes) -> bool:
    return payload.startswith((b"070701", b"070702", b"070707", b"\xc7q", b"q\xc7"))


def _payload_format_hint(payload: bytes) -> str:
    if not payload:
        return "empty payload"
    if payload.startswith(b"\x1f\x8b"):
        return "gzip-compressed payload"
    if payload.startswith(b"\xfd7zXZ\x00"):
        return "xz-compressed payload"
    if payload.startswith(b"BZh"):
        return "bzip2-compressed payload"
    if payload.startswith(b"\x28\xb5\x2f\xfd"):
        return "zstd-compressed payload"
    if _payload_starts_with_cpio_header(payload):
        return "cpio payload"
    return f"unrecognized payload starting with {payload[:8].hex()}"


def _decompress_rpm_payload(payload: bytes) -> bytes | None:
    if payload.startswith(b"\x1f\x8b"):
        return gzip.decompress(payload)
    if payload.startswith(b"\xfd7zXZ\x00"):
        return lzma.decompress(payload)
    if payload.startswith(b"BZh"):
        return bz2.decompress(payload)
    if payload.startswith(b"\x28\xb5\x2f\xfd"):
        zstd_path = shutil.which("zstd")
        if not zstd_path:
            return None
        result = subprocess.run(
            [zstd_path, "-dc"],
            input=payload,
            capture_output=True,
            env=_subprocess_c_locale_env(),
            check=False,
        )
        if result.returncode != 0:
            return None
        return bytes(result.stdout or b"")
    return None


def _safe_extract_tar_payload(payload: bytes, destination: Path, *, label: str) -> None:
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
            for member in archive.getmembers():
                member_path = (destination / member.name).resolve()
                if (
                    destination.resolve() not in member_path.parents
                    and member_path != destination.resolve()
                ):
                    raise StabilityTestError(
                        f"refusing to extract suspicious {label} member: {member.name}"
                    )
                if member.isdir():
                    member_path.mkdir(parents=True, exist_ok=True)
                    continue
                if member.issym():
                    link_target = member.linkname
                    if os.path.isabs(link_target) or ".." in Path(link_target).parts:
                        raise StabilityTestError(
                            f"refusing to extract suspicious {label} symlink: {member.name}"
                        )
                    member_path.parent.mkdir(parents=True, exist_ok=True)
                    if member_path.exists() or member_path.is_symlink():
                        member_path.unlink()
                    member_path.symlink_to(link_target)
                    continue
                if not member.isfile():
                    raise StabilityTestError(
                        f"refusing to extract unsupported {label} member: {member.name}"
                    )
                source = archive.extractfile(member)
                if source is None:
                    raise StabilityTestError(
                        f"failed to read {label} member: {member.name}"
                    )
                member_path.parent.mkdir(parents=True, exist_ok=True)
                with source, member_path.open("wb") as output:
                    shutil.copyfileobj(source, output)
                member_path.chmod(member.mode & 0o777)
    except tarfile.TarError as exc:
        raise StabilityTestError(
            f"failed to extract {label} tar payload: {exc}"
        ) from exc


def _extract_cpio_payload(payload: bytes, destination: Path, *, label: str) -> None:
    cpio_path = shutil.which("cpio")
    if not cpio_path:
        raise StabilityTestError(
            f"{label} produced a cpio payload, but cpio is not installed"
        )
    cpio_result = subprocess.run(
        [cpio_path, "-idm", "--quiet"],
        cwd=destination,
        input=payload,
        capture_output=True,
        env=_subprocess_c_locale_env(),
        check=False,
    )
    if cpio_result.returncode != 0:
        detail = (
            cpio_result.stderr.decode("utf-8", errors="replace").strip()
            or f"exit code {cpio_result.returncode}"
        )
        raise StabilityTestError(f"cpio failed while extracting {label}: {detail}")


def _extract_rpm2cpio_payload(payload: bytes, destination: Path, *, label: str) -> bool:
    if _payload_starts_with_cpio_header(payload):
        _extract_cpio_payload(payload, destination, label=label)
        return True

    decompressed_payload = _decompress_rpm_payload(payload)
    if decompressed_payload is not None:
        if _payload_starts_with_cpio_header(decompressed_payload):
            _extract_cpio_payload(decompressed_payload, destination, label=label)
            return True
        try:
            _safe_extract_tar_payload(decompressed_payload, destination, label=label)
            return True
        except StabilityTestError:
            return False

    try:
        _safe_extract_tar_payload(payload, destination, label=label)
        return True
    except StabilityTestError:
        return False


def _extract_q2rtx_archive(archive_path: Path, install_dir: Path) -> None:
    install_dir.parent.mkdir(parents=True, exist_ok=True)
    claim_desktop_user_ownership(install_dir.parent, include_parents=True)
    with tempfile.TemporaryDirectory(
        prefix="q2rtx-extract-",
        dir=str(install_dir.parent),
    ) as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        try:
            with tarfile.open(archive_path, "r:gz") as archive:
                for member in archive.getmembers():
                    member_path = (temp_dir / member.name).resolve()
                    if (
                        temp_dir.resolve() not in member_path.parents
                        and member_path != temp_dir.resolve()
                    ):
                        raise StabilityTestError(
                            f"refusing to extract suspicious archive member: {member.name}"
                        )
                    if member.isdir():
                        member_path.mkdir(parents=True, exist_ok=True)
                        continue
                    if not member.isfile():
                        raise StabilityTestError(
                            f"refusing to extract unsupported archive member: {member.name}"
                        )
                    member_path.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise StabilityTestError(
                            f"failed to read archive member: {member.name}"
                        )
                    with source, member_path.open("wb") as destination:
                        shutil.copyfileobj(source, destination)
                    member_path.chmod(member.mode & 0o777)
        except (tarfile.TarError, OSError) as exc:
            raise StabilityTestError(
                f"failed to extract {archive_path}: {exc}"
            ) from exc

        entries = [path for path in temp_dir.iterdir()]
        if len(entries) == 1 and entries[0].is_dir():
            extracted_root = entries[0]
        else:
            extracted_root = temp_dir

        if install_dir.exists():
            shutil.rmtree(install_dir)
        install_dir.mkdir(parents=True, exist_ok=True)
        for item in extracted_root.iterdir():
            destination = install_dir / item.name
            if destination.exists():
                if destination.is_dir() and not destination.is_symlink():
                    shutil.rmtree(destination)
                else:
                    destination.unlink()
            shutil.move(str(item), str(destination))
    claim_desktop_user_ownership(install_dir, recursive=True)
