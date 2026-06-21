from __future__ import annotations

import io
import os
from pathlib import Path
import shutil
import tarfile
import tempfile

from common.penguin_burner_paths import claim_desktop_user_ownership

from .models import StabilityTestError


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


def _archive_member_baseq2_relative_name(member_name: str) -> str | None:
    parts = Path(member_name).parts
    try:
        baseq2_index = parts.index("baseq2")
    except ValueError:
        return None
    relative_parts = parts[baseq2_index:]
    if len(relative_parts) < 2:
        return None
    return "/".join(relative_parts)


def _extract_q2rtx_data_files(
    archive_path: Path,
    install_dir: Path,
    *,
    required_files: tuple[str, ...],
) -> None:
    required = set(required_files)
    found: set[str] = set()
    baseq2_dir = install_dir / "baseq2"
    baseq2_dir.mkdir(parents=True, exist_ok=True)
    claim_desktop_user_ownership(baseq2_dir, include_parents=True)

    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                relative_name = _archive_member_baseq2_relative_name(member.name)
                if relative_name not in required:
                    continue
                member_path = (install_dir / relative_name).resolve()
                if (
                    install_dir.resolve() not in member_path.parents
                    and member_path != install_dir.resolve()
                ):
                    raise StabilityTestError(
                        f"refusing to extract suspicious data member: {member.name}"
                    )
                source = archive.extractfile(member)
                if source is None:
                    raise StabilityTestError(
                        f"failed to read data member: {member.name}"
                    )
                member_path.parent.mkdir(parents=True, exist_ok=True)
                with source, member_path.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
                member_path.chmod(member.mode & 0o777)
                found.add(relative_name)
    except (tarfile.TarError, OSError) as exc:
        raise StabilityTestError(
            f"failed to extract Q2RTX shareware data from {archive_path}: {exc}"
        ) from exc

    missing = sorted(required - found)
    if missing:
        raise StabilityTestError(
            "Q2RTX shareware data archive is missing required files: "
            + ", ".join(missing)
        )
    claim_desktop_user_ownership(baseq2_dir, recursive=True)
