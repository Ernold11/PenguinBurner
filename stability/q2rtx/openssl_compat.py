from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from penguin_burner_paths import claim_desktop_user_ownership

from .archive_extraction import (
    _extract_rpm2cpio_payload,
    _payload_format_hint,
    _subprocess_c_locale_env,
)
from .constants import (
    OPENSSL_111_COMPAT_RPM_INDEX_URLS,
    OPENSSL_111_REQUIRED_LIBS,
    OPENSSL_111_VERSION,
)
from .downloader import (
    _download_file_from_urls,
    _download_text,
    _format_attempt_errors,
    _join_mirror_url,
    _unique_https_urls,
)
from .models import StabilityTestError
from .paths import default_q2rtx_compat_dir, default_q2rtx_install_cache_dir
from .progress import DependencyProgressCallback, _emit_dependency_progress


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


def _openssl_compat_rpm_archive_path(cache_dir: Path, rpm_name: str) -> Path:
    return cache_dir / rpm_name


def _openssl_compat_rpm_urls(rpm_name: str, first_index_url: str) -> tuple[str, ...]:
    return _unique_https_urls(
        tuple(
            _join_mirror_url(index_url, rpm_name)
            for index_url in (first_index_url, *OPENSSL_111_COMPAT_RPM_INDEX_URLS)
        )
    )


def _fetch_latest_openssl_compat_rpm_metadata() -> tuple[str, tuple[str, ...]]:
    errors: list[tuple[str, str]] = []
    for index_url in _unique_https_urls(OPENSSL_111_COMPAT_RPM_INDEX_URLS):
        try:
            text = _download_text(index_url)
        except StabilityTestError as exc:
            errors.append((index_url, str(exc)))
            continue
        matches = re.findall(
            r'href="(compat-openssl11-[^"]+\.x86_64\.rpm)"',
            text,
        )
        if matches:
            rpm_name = matches[-1]
            return rpm_name, _openssl_compat_rpm_urls(rpm_name, index_url)
        errors.append(
            (
                index_url,
                "could not find compat-openssl11 x86_64 RPM in package index",
            )
        )
    raise StabilityTestError(
        "could not find compat-openssl11 x86_64 RPM in any CentOS Stream "
        f"AppStream package index; tried {_format_attempt_errors(errors)}"
    )


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
    rpm_name, rpm_urls = _fetch_latest_openssl_compat_rpm_metadata()
    rpm_path = _openssl_compat_rpm_archive_path(cache_dir, rpm_name)
    if not rpm_path.is_file():
        _download_file_from_urls(
            rpm_urls,
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
