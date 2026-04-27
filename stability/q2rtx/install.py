from __future__ import annotations

import bz2
import gzip
import io
import json
import lzma
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from typing import Callable
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from penguin_burner_paths import claim_desktop_user_ownership
from subprocess_locale import stable_subprocess_env

from .assets import _effective_q2rtx_xdg_dir, resolve_q2rtx_executable
from .constants import (
    DEFAULT_INSTALL_CACHE_DIR,
    DEFAULT_INSTALL_DATA_DIR,
    OPENSSL_111_COMPAT_RPM_INDEX_URL,
    OPENSSL_111_REQUIRED_LIBS,
    OPENSSL_111_VERSION,
    Q2RTX_GITHUB_RELEASES_URL,
    Q2RTX_RELEASES_API_URL,
)
from .models import Q2RTXInstallResult, StabilityTestError


DependencyProgressCallback = Callable[[dict], None]


def default_q2rtx_install_data_dir() -> Path:
    xdg_data_home = _effective_q2rtx_xdg_dir("data")
    if xdg_data_home is not None:
        return xdg_data_home / "PenguinBurner" / "q2rtx"
    return DEFAULT_INSTALL_DATA_DIR


def default_q2rtx_install_cache_dir() -> Path:
    xdg_cache_home = _effective_q2rtx_xdg_dir("cache")
    if xdg_cache_home is not None:
        return xdg_cache_home / "PenguinBurner" / "q2rtx"
    return DEFAULT_INSTALL_CACHE_DIR


def default_q2rtx_compat_dir() -> Path:
    return (
        default_q2rtx_install_data_dir() / "compat" / f"openssl-{OPENSSL_111_VERSION}"
    )


def _format_size(value: float) -> str:
    units = ("B", "KiB", "MiB", "GiB")
    amount = float(value)
    unit = units[0]
    for unit in units:
        if abs(amount) < 1024.0 or unit == units[-1]:
            break
        amount /= 1024.0
    if unit == "B":
        return f"{int(amount)}{unit}"
    return f"{amount:.1f}{unit}"


def _require_https_url(url: str) -> str:
    parsed = urllib_parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise StabilityTestError(f"refusing non-HTTPS download URL: {url}")
    return url


def _emit_dependency_progress(
    callback: DependencyProgressCallback | None,
    percent: float,
    detail: str,
    **payload,
) -> None:
    if callback is None:
        return
    try:
        percent_value = max(0.0, min(100.0, float(percent)))
    except (TypeError, ValueError):
        percent_value = 0.0
    data = {
        "label": "Downloading dependencies",
        "percent": round(percent_value, 1),
        "detail": str(detail),
    }
    data.update(payload)
    callback(data)


def _progress_range_value(
    start_pct: float,
    end_pct: float,
    local_percent: float,
) -> float:
    start = max(0.0, min(100.0, float(start_pct)))
    end = max(0.0, min(100.0, float(end_pct)))
    local = max(0.0, min(100.0, float(local_percent)))
    return start + ((end - start) * (local / 100.0))


def _download_text(url: str) -> str:
    url = _require_https_url(url)
    request = urllib_request.Request(
        url,
        headers={
            "Accept": "text/plain",
            "User-Agent": "PenguinBurner-Q2RTX-Installer",
        },
    )
    try:
        with urllib_request.urlopen(request, timeout=30.0) as response:  # nosec B310
            return response.read().decode("utf-8", errors="replace")
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        if detail:
            raise StabilityTestError(f"download failed ({exc.code}): {detail}") from exc
        raise StabilityTestError(f"download failed with status {exc.code}") from exc
    except urllib_error.URLError as exc:
        raise StabilityTestError(f"download failed: {exc}") from exc


def _github_json(url: str) -> dict:
    url = _require_https_url(url)
    request = urllib_request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "PenguinBurner-Q2RTX-Installer",
        },
    )
    try:
        with urllib_request.urlopen(request, timeout=30.0) as response:  # nosec B310
            payload = response.read()
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        if detail:
            raise StabilityTestError(
                f"GitHub API request failed ({exc.code}): {detail}"
            ) from exc
        raise StabilityTestError(
            f"GitHub API request failed with status {exc.code}"
        ) from exc
    except urllib_error.URLError as exc:
        raise StabilityTestError(f"failed to contact GitHub: {exc}") from exc

    try:
        data = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise StabilityTestError("GitHub API returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise StabilityTestError("GitHub API returned an unexpected payload")
    return data


def fetch_latest_q2rtx_release_metadata() -> tuple[str, str, str]:
    data = _github_json(Q2RTX_RELEASES_API_URL)
    tag_name = str(data.get("tag_name", "")).strip()
    if not tag_name:
        raise StabilityTestError(
            f"latest Q2RTX release metadata missing tag_name; see {Q2RTX_GITHUB_RELEASES_URL}"
        )

    assets = data.get("assets")
    if not isinstance(assets, list):
        raise StabilityTestError(
            f"latest Q2RTX release metadata missing assets; see {Q2RTX_GITHUB_RELEASES_URL}"
        )

    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name", "")).strip()
        url = str(asset.get("browser_download_url", "")).strip()
        if name.endswith("-linux.tar.gz") and url:
            return tag_name, name, _require_https_url(url)

    raise StabilityTestError(
        f"no Linux tar.gz asset found in the latest Q2RTX release; see {Q2RTX_GITHUB_RELEASES_URL}"
    )


def _download_file(
    url: str,
    destination: Path,
    *,
    label: str,
    show_progress: bool,
    progress_callback: DependencyProgressCallback | None = None,
    progress_start_pct: float = 0.0,
    progress_end_pct: float = 100.0,
) -> None:
    url = _require_https_url(url)
    destination.parent.mkdir(parents=True, exist_ok=True)
    claim_desktop_user_ownership(destination.parent, include_parents=True)
    request = urllib_request.Request(
        url,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": "PenguinBurner-Q2RTX-Installer",
        },
    )
    tmp_path = destination.with_suffix(destination.suffix + ".part")
    try:
        with urllib_request.urlopen(request, timeout=60.0) as response:  # nosec B310
            total_bytes_header = response.headers.get("Content-Length", "").strip()
            total_bytes = int(total_bytes_header) if total_bytes_header.isdigit() else 0
            chunk_size = 1024 * 1024
            downloaded_bytes = 0
            started_monotonic = time.monotonic()
            last_update_monotonic = started_monotonic
            if show_progress:
                print(
                    f"Downloading {label} to {destination}...",
                    flush=True,
                )
            _emit_dependency_progress(
                progress_callback,
                progress_start_pct,
                f"Downloading {label}",
                label=label,
                destination=str(destination),
            )
            with tmp_path.open("wb") as handle:
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    handle.write(chunk)
                    downloaded_bytes += len(chunk)
                    now_monotonic = time.monotonic()
                    if now_monotonic - last_update_monotonic >= 0.25:
                        elapsed_s = max(now_monotonic - started_monotonic, 0.001)
                        speed_bps = downloaded_bytes / elapsed_s
                        if total_bytes > 0:
                            percent = (downloaded_bytes / total_bytes) * 100.0
                            overall_percent = _progress_range_value(
                                progress_start_pct,
                                progress_end_pct,
                                percent,
                            )
                            status = (
                                f"\r  {percent:5.1f}%  "
                                f"{_format_size(downloaded_bytes)}/"
                                f"{_format_size(total_bytes)}  "
                                f"{_format_size(speed_bps)}/s"
                            )
                        else:
                            overall_percent = progress_start_pct
                            status = (
                                f"\r  {_format_size(downloaded_bytes)}  "
                                f"{_format_size(speed_bps)}/s"
                            )
                        _emit_dependency_progress(
                            progress_callback,
                            overall_percent,
                            f"Downloading {label}",
                            label=label,
                            downloaded_bytes=downloaded_bytes,
                            total_bytes=total_bytes,
                            destination=str(destination),
                        )
                        if show_progress:
                            print(status, end="", flush=True)
                        last_update_monotonic = now_monotonic
            if show_progress:
                elapsed_s = max(time.monotonic() - started_monotonic, 0.001)
                speed_bps = downloaded_bytes / elapsed_s
                if total_bytes > 0:
                    status = (
                        f"\r  100.0%  {_format_size(downloaded_bytes)}/"
                        f"{_format_size(total_bytes)}  {_format_size(speed_bps)}/s"
                    )
                else:
                    status = f"\r  {_format_size(downloaded_bytes)}  {_format_size(speed_bps)}/s"
                print(status, flush=True)
            _emit_dependency_progress(
                progress_callback,
                progress_end_pct,
                f"Downloaded {label}",
                label=label,
                downloaded_bytes=downloaded_bytes,
                total_bytes=total_bytes,
                destination=str(destination),
            )
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        if detail:
            raise StabilityTestError(f"download failed ({exc.code}): {detail}") from exc
        raise StabilityTestError(f"download failed with status {exc.code}") from exc
    except urllib_error.URLError as exc:
        raise StabilityTestError(f"download failed: {exc}") from exc
    except OSError as exc:
        raise StabilityTestError(
            f"failed writing download to {tmp_path}: {exc}"
        ) from exc

    try:
        tmp_path.replace(destination)
    except OSError as exc:
        raise StabilityTestError(
            f"failed to finalize download at {destination}: {exc}"
        ) from exc
    claim_desktop_user_ownership(destination)


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


def _openssl_compat_rpm_archive_path(cache_dir: Path, rpm_name: str) -> Path:
    return cache_dir / rpm_name


def _fetch_latest_openssl_compat_rpm_metadata() -> tuple[str, str]:
    text = _download_text(OPENSSL_111_COMPAT_RPM_INDEX_URL)
    matches = re.findall(
        r'href="(compat-openssl11-[^"]+\.x86_64\.rpm)"',
        text,
    )
    if not matches:
        raise StabilityTestError(
            "could not find compat-openssl11 x86_64 RPM in the CentOS Stream AppStream index"
        )
    rpm_name = matches[-1]
    return rpm_name, OPENSSL_111_COMPAT_RPM_INDEX_URL + rpm_name


def _copy_preserving_link(src: Path, dst: Path) -> None:
    if dst.exists() or dst.is_symlink():
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    dst.parent.mkdir(parents=True, exist_ok=True)
    claim_desktop_user_ownership(dst.parent, include_parents=True)
    if src.is_symlink():
        dst.symlink_to(os.readlink(src))
        claim_desktop_user_ownership(dst)
        return
    if src.is_dir():
        shutil.copytree(src, dst, symlinks=True)
        claim_desktop_user_ownership(dst, recursive=True)
        return
    shutil.copy2(src, dst)
    claim_desktop_user_ownership(dst)


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


def _ldconfig_openssl_111_dirs() -> list[Path]:
    ldconfig_path = shutil.which("ldconfig")
    if not ldconfig_path:
        return []
    try:
        result = subprocess.run(
            [ldconfig_path, "-p"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_subprocess_c_locale_env(),
        )
    except OSError:
        return []
    if result.returncode != 0:
        return []

    dirs: list[Path] = []
    for line in result.stdout.splitlines():
        if "libssl.so.1.1" not in line and "libcrypto.so.1.1" not in line:
            continue
        _, _, path_text = line.partition("=>")
        path_text = path_text.strip()
        if not path_text:
            continue
        directory = Path(path_text).parent
        if directory not in dirs:
            dirs.append(directory)
    return dirs


def _copy_system_openssl_111_libs(compat_root: Path) -> Path | None:
    candidate_dirs = [
        Path("/usr/lib"),
        Path("/usr/lib64"),
        Path("/usr/lib/x86_64-linux-gnu"),
        Path("/lib"),
        Path("/lib64"),
        Path("/lib/x86_64-linux-gnu"),
        Path("/usr/local/lib"),
        Path("/usr/local/lib64"),
    ]
    candidate_dirs.extend(
        directory for directory in _ldconfig_openssl_111_dirs() if directory.exists()
    )
    source_dir = next(
        (
            directory
            for directory in candidate_dirs
            if all((directory / name).exists() for name in OPENSSL_111_REQUIRED_LIBS)
        ),
        None,
    )
    if source_dir is None:
        return None

    lib_dir = compat_root / "lib"
    lib_dir.mkdir(parents=True, exist_ok=True)
    claim_desktop_user_ownership(lib_dir, include_parents=True)
    for required_name in OPENSSL_111_REQUIRED_LIBS:
        for entry in sorted(source_dir.glob(required_name + "*")):
            _copy_preserving_link(entry, lib_dir / entry.name)
    return lib_dir


def _extract_compat_openssl_with_bsdtar(rpm_path: Path, temp_dir: Path) -> bool:
    bsdtar_path = shutil.which("bsdtar")
    if not bsdtar_path:
        return False
    result = subprocess.run(
        [bsdtar_path, "-xf", str(rpm_path), "-C", str(temp_dir)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_subprocess_c_locale_env(),
        check=False,
    )
    if result.returncode == 0:
        return True
    detail = (result.stderr or result.stdout or "").strip()
    raise StabilityTestError(
        f"bsdtar failed while extracting {rpm_path.name}: {detail or result.returncode}"
    )


def _extract_compat_openssl_rpm(rpm_path: Path, compat_root: Path) -> Path:
    rpm2cpio_path = shutil.which("rpm2cpio")

    with tempfile.TemporaryDirectory(
        prefix="compat-openssl11-",
        dir=str(rpm_path.parent),
    ) as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        if rpm2cpio_path:
            try:
                rpm2cpio_result = subprocess.run(
                    [rpm2cpio_path, str(rpm_path)],
                    capture_output=True,
                    text=False,
                    env=_subprocess_c_locale_env(),
                    check=False,
                )
            except OSError as exc:
                raise StabilityTestError(f"failed to start rpm2cpio: {exc}") from exc

            rpm2cpio_stderr = rpm2cpio_result.stderr.decode("utf-8", errors="replace")
            if rpm2cpio_result.returncode != 0:
                detail = (
                    rpm2cpio_stderr.strip() or f"exit code {rpm2cpio_result.returncode}"
                )
                raise StabilityTestError(
                    f"rpm2cpio failed while extracting {rpm_path.name}: {detail}"
                )
            payload = bytes(rpm2cpio_result.stdout or b"")
            if _extract_rpm2cpio_payload(payload, temp_dir, label=rpm_path.name):
                pass
            elif _extract_compat_openssl_with_bsdtar(rpm_path, temp_dir):
                pass
            else:
                raise StabilityTestError(
                    f"rpm2cpio produced {_payload_format_hint(payload)} for "
                    f"{rpm_path.name}; refusing to feed it to cpio as a raw cpio archive"
                )
        elif not _extract_compat_openssl_with_bsdtar(rpm_path, temp_dir):
            raise StabilityTestError(
                "extracting the compat-openssl11 RPM requires rpm2cpio, bsdtar, "
                "or a system OpenSSL 1.1 package that provides libssl.so.1.1"
            )

        lib_source_dir = temp_dir / "usr" / "lib64"
        if not lib_source_dir.is_dir():
            raise StabilityTestError(
                f"{rpm_path.name} did not contain the expected usr/lib64 payload"
            )

        compat_root.mkdir(parents=True, exist_ok=True)
        claim_desktop_user_ownership(compat_root, include_parents=True)
        lib_dir = compat_root / "lib"
        lib_dir.mkdir(parents=True, exist_ok=True)
        claim_desktop_user_ownership(lib_dir, include_parents=True)

        lib_entries: list[Path] = []
        for prefix in OPENSSL_111_REQUIRED_LIBS:
            lib_entries.extend(sorted(lib_source_dir.glob(prefix + "*")))
        if not lib_entries:
            raise StabilityTestError(
                f"{rpm_path.name} did not contain the required OpenSSL 1.1 shared libraries"
            )

        unique_entries: list[Path] = []
        seen_entries: set[Path] = set()
        for entry in lib_entries:
            if entry in seen_entries:
                continue
            seen_entries.add(entry)
            unique_entries.append(entry)
        unique_entries.sort(key=lambda path: (path.is_symlink(), path.name))
        for entry in unique_entries:
            _copy_preserving_link(entry, lib_dir / entry.name)

        engines_source_dir = lib_source_dir / "engines-1.1"
        if engines_source_dir.is_dir():
            _copy_preserving_link(engines_source_dir, compat_root / "engines-1.1")

        conf_source = temp_dir / "etc" / "pki" / "tls" / "openssl11.cnf"
        if conf_source.is_file():
            _copy_preserving_link(conf_source, compat_root / "ssl" / "openssl11.cnf")

    if not all((lib_dir / name).exists() for name in OPENSSL_111_REQUIRED_LIBS):
        raise StabilityTestError(
            f"compat-openssl11 extraction completed but required libs were not found under {lib_dir}"
        )
    claim_desktop_user_ownership(compat_root, recursive=True)
    return lib_dir


def _ensure_openssl_111_compat_libs(
    *,
    show_progress: bool,
    progress_callback: DependencyProgressCallback | None = None,
    progress_start_pct: float = 85.0,
    progress_end_pct: float = 98.0,
) -> Path:
    compat_root = default_q2rtx_compat_dir()
    lib_dir = compat_root / "lib"
    if all((lib_dir / name).is_file() for name in OPENSSL_111_REQUIRED_LIBS):
        if show_progress:
            print(
                f"Using cached OpenSSL {OPENSSL_111_VERSION} compatibility libs from {lib_dir}",
                flush=True,
            )
        _emit_dependency_progress(
            progress_callback,
            progress_end_pct,
            "OpenSSL compatibility libraries are already available",
            path=str(lib_dir),
        )
        return lib_dir

    system_lib_dir = _copy_system_openssl_111_libs(compat_root)
    if system_lib_dir is not None:
        if show_progress:
            print(
                f"Using system OpenSSL {OPENSSL_111_VERSION} compatibility libs from {system_lib_dir}",
                flush=True,
            )
        _emit_dependency_progress(
            progress_callback,
            progress_end_pct,
            "Using system OpenSSL compatibility libraries",
            path=str(system_lib_dir),
        )
        return system_lib_dir

    _emit_dependency_progress(
        progress_callback,
        progress_start_pct,
        "Checking OpenSSL compatibility libraries",
    )
    cache_dir = default_q2rtx_install_cache_dir() / "compat-rpms"
    rpm_name, rpm_url = _fetch_latest_openssl_compat_rpm_metadata()
    rpm_path = _openssl_compat_rpm_archive_path(cache_dir, rpm_name)
    if not rpm_path.is_file():
        _download_file(
            rpm_url,
            rpm_path,
            label=f"compat-openssl11 RPM ({rpm_name})",
            show_progress=show_progress,
            progress_callback=progress_callback,
            progress_start_pct=progress_start_pct,
            progress_end_pct=progress_start_pct
            + ((progress_end_pct - progress_start_pct) * 0.70),
        )
    elif show_progress:
        print(f"Using cached compat-openssl11 RPM {rpm_path}", flush=True)
    if rpm_path.is_file():
        _emit_dependency_progress(
            progress_callback,
            progress_start_pct + ((progress_end_pct - progress_start_pct) * 0.70),
            "OpenSSL compatibility RPM is available",
            path=str(rpm_path),
        )

    if show_progress:
        print(
            f"Extracting sandboxed OpenSSL 1.1 compatibility libs from {rpm_name}...",
            flush=True,
        )
    _emit_dependency_progress(
        progress_callback,
        progress_start_pct + ((progress_end_pct - progress_start_pct) * 0.80),
        "Extracting OpenSSL compatibility libraries",
    )
    lib_dir = _extract_compat_openssl_rpm(rpm_path, compat_root)

    if show_progress:
        print(
            f"Installed OpenSSL {OPENSSL_111_VERSION} compatibility libs to {lib_dir}",
            flush=True,
        )
    _emit_dependency_progress(
        progress_callback,
        progress_end_pct,
        "Installed OpenSSL compatibility libraries",
        path=str(lib_dir),
    )
    return lib_dir


def _detect_missing_shared_libraries(
    executable_path: Path,
    *,
    env: dict[str, str] | None = None,
) -> list[str]:
    ldd_path = shutil.which("ldd")
    if not ldd_path:
        return []
    try:
        result = subprocess.run(
            [ldd_path, str(executable_path)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=stable_subprocess_env(env),
        )
    except OSError:
        return []

    missing: list[str] = []
    for line in result.stdout.splitlines():
        if "=> not found" not in line:
            continue
        name = line.split("=>", 1)[0].strip()
        if name:
            missing.append(name)
    return missing


def _prepend_library_path(env: dict[str, str], lib_dir: Path) -> dict[str, str]:
    updated = dict(env)
    existing = updated.get("LD_LIBRARY_PATH", "").strip()
    if existing:
        updated["LD_LIBRARY_PATH"] = f"{lib_dir}:{existing}"
    else:
        updated["LD_LIBRARY_PATH"] = str(lib_dir)
    return updated


def _prepare_q2rtx_runtime_env(
    executable_path: Path,
    *,
    show_progress: bool,
    progress_callback: DependencyProgressCallback | None = None,
    progress_start_pct: float = 85.0,
    progress_end_pct: float = 98.0,
) -> tuple[dict[str, str], Path | None]:
    env = dict(os.environ)
    env.setdefault("SDL_VIDEO_ALLOW_SCREENSAVER", "1")

    missing = _detect_missing_shared_libraries(executable_path, env=env)
    if not missing:
        _emit_dependency_progress(
            progress_callback,
            progress_end_pct,
            "Q2RTX runtime libraries are available",
            executable=str(executable_path),
        )
        return env, None

    missing_set = set(missing)
    openssl_missing = set(OPENSSL_111_REQUIRED_LIBS) & missing_set
    remaining_missing = missing_set - set(OPENSSL_111_REQUIRED_LIBS)
    if remaining_missing:
        raise StabilityTestError(
            "Q2RTX is missing required shared libraries: "
            + ", ".join(sorted(remaining_missing))
        )
    if not openssl_missing:
        raise StabilityTestError(
            "Q2RTX is missing required shared libraries: "
            + ", ".join(sorted(missing_set))
        )

    compat_lib_dir = _ensure_openssl_111_compat_libs(
        show_progress=show_progress,
        progress_callback=progress_callback,
        progress_start_pct=progress_start_pct,
        progress_end_pct=progress_end_pct,
    )
    env = _prepend_library_path(env, compat_lib_dir)
    compat_root = compat_lib_dir.parent
    compat_conf = compat_root / "ssl" / "openssl11.cnf"
    compat_engines = compat_root / "engines-1.1"
    if compat_conf.is_file():
        env["OPENSSL_CONF"] = str(compat_conf)
    if compat_engines.is_dir():
        env["OPENSSL_ENGINES"] = str(compat_engines)
    missing_after = _detect_missing_shared_libraries(executable_path, env=env)
    if missing_after:
        raise StabilityTestError(
            "Q2RTX is still missing shared libraries after installing compatibility libs: "
            + ", ".join(sorted(missing_after))
        )
    return env, compat_lib_dir


def install_latest_q2rtx(
    *,
    data_dir: Path | None = None,
    cache_dir: Path | None = None,
    show_progress: bool = True,
    progress_callback: DependencyProgressCallback | None = None,
) -> Q2RTXInstallResult:
    if show_progress:
        print("Q2RTX install: fetching latest release metadata...", flush=True)
    _emit_dependency_progress(
        progress_callback,
        2.0,
        "Fetching Q2RTX release metadata",
    )
    tag_name, asset_name, asset_url = fetch_latest_q2rtx_release_metadata()
    version = tag_name[1:] if tag_name.startswith("v") else tag_name

    resolved_data_dir = (
        data_dir.expanduser().resolve()
        if data_dir is not None
        else default_q2rtx_install_data_dir()
    )
    resolved_cache_dir = (
        cache_dir.expanduser().resolve()
        if cache_dir is not None
        else default_q2rtx_install_cache_dir()
    )
    resolved_data_dir.mkdir(parents=True, exist_ok=True)
    resolved_cache_dir.mkdir(parents=True, exist_ok=True)
    claim_desktop_user_ownership(resolved_data_dir, include_parents=True)
    claim_desktop_user_ownership(resolved_cache_dir, include_parents=True)
    archive_path = resolved_cache_dir / asset_name
    install_dir = resolved_data_dir / version
    if show_progress:
        print(
            f"Q2RTX install: latest={version} asset={asset_name}",
            flush=True,
        )
    _emit_dependency_progress(
        progress_callback,
        8.0,
        f"Found Q2RTX {version}",
        version=version,
        asset=asset_name,
    )

    if not archive_path.is_file():
        _download_file(
            asset_url,
            archive_path,
            label=f"Q2RTX {version} Linux build",
            show_progress=show_progress,
            progress_callback=progress_callback,
            progress_start_pct=10.0,
            progress_end_pct=72.0,
        )
    elif show_progress:
        print(f"Using cached Q2RTX archive {archive_path}", flush=True)
    if archive_path.is_file():
        _emit_dependency_progress(
            progress_callback,
            72.0,
            "Q2RTX archive is available",
            path=str(archive_path),
        )
    if show_progress:
        print(f"Q2RTX install: extracting archive to {install_dir}...", flush=True)
    _emit_dependency_progress(
        progress_callback,
        78.0,
        "Extracting Q2RTX archive",
        path=str(install_dir),
    )
    _extract_q2rtx_archive(archive_path, install_dir)
    _emit_dependency_progress(
        progress_callback,
        84.0,
        "Q2RTX archive extracted",
        path=str(install_dir),
    )

    executable_path, workdir = resolve_q2rtx_executable(
        q2rtx_dir=install_dir,
        q2rtx_binary=None,
    )
    if workdir != install_dir:
        install_dir = workdir

    pak0_path = install_dir / "baseq2" / "pak0.pak"
    if not pak0_path.is_file():
        raise StabilityTestError(
            f"installed Q2RTX build is missing expected demo data: {pak0_path}"
        )
    if show_progress:
        print(
            f"Q2RTX install: preparing runtime libraries for {executable_path}...",
            flush=True,
        )
    _emit_dependency_progress(
        progress_callback,
        86.0,
        "Preparing Q2RTX runtime libraries",
        executable=str(executable_path),
    )
    _prepare_q2rtx_runtime_env(
        executable_path,
        show_progress=show_progress,
        progress_callback=progress_callback,
        progress_start_pct=86.0,
        progress_end_pct=98.0,
    )
    claim_desktop_user_ownership(resolved_data_dir, recursive=True)
    claim_desktop_user_ownership(resolved_cache_dir, recursive=True)
    if show_progress:
        print(
            f"Q2RTX install: ready install_dir={install_dir} executable={executable_path}",
            flush=True,
        )
    _emit_dependency_progress(
        progress_callback,
        100.0,
        "Q2RTX dependencies are ready",
        install_dir=str(install_dir),
        executable=str(executable_path),
    )

    return Q2RTXInstallResult(
        version=version,
        asset_name=asset_name,
        asset_url=asset_url,
        archive_path=archive_path,
        install_dir=install_dir,
        executable_path=executable_path,
    )
