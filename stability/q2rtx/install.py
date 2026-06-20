from __future__ import annotations

from pathlib import Path
import shutil

from common.penguin_burner_paths import claim_desktop_user_ownership

from .archive_extraction import _extract_q2rtx_archive, _extract_q2rtx_data_files
from .assets import _default_q2rtx_roots, resolve_q2rtx_executable
from .constants import (
    DEFAULT_LOG_DIR,
    PB_Q2RTX_ASSET_PREFIX,
    PB_Q2RTX_ASSET_SUFFIX,
    PB_Q2RTX_LATEST_RELEASE_API_URL,
    PB_Q2RTX_REPOSITORY,
    Q2RTX_REQUIRED_DATA_FILES,
    Q2RTX_SHAREWARE_DATA_ASSET_NAME,
    Q2RTX_SHAREWARE_DATA_ASSET_URL,
    Q2RTX_SHAREWARE_DATA_VERSION,
)
from .downloader import (
    _download_file_from_urls,
    _github_json,
)
from .models import Q2RTXInstallResult, StabilityTestError
from .paths import (
    default_q2rtx_install_cache_dir,
    default_q2rtx_install_data_dir,
)
from .progress import DependencyProgressCallback, _emit_dependency_progress
from .runtime_env import _prepare_q2rtx_runtime_env

__all__ = [
    "clear_q2rtx_stability_logs",
    "default_q2rtx_install_cache_dir",
    "default_q2rtx_install_data_dir",
    "fetch_latest_q2rtx_release_metadata",
    "install_latest_q2rtx",
]


def clear_q2rtx_stability_logs(
    log_dir: Path | None = None,
    *,
    show_progress: bool = True,
) -> int:
    """Remove generated Q2RTX stability logs before a managed reinstall."""
    resolved_log_dir = (
        log_dir.expanduser().resolve() if log_dir is not None else DEFAULT_LOG_DIR
    )
    if not resolved_log_dir.is_dir():
        return 0
    removed = 0
    for path in resolved_log_dir.glob("q2rtx-stability-*.log"):
        if not path.is_file():
            continue
        try:
            path.unlink()
        except OSError:
            continue
        removed += 1
    if show_progress and removed:
        print(
            f"Q2RTX install: removed old stability logs count={removed} "
            f"dir={resolved_log_dir}",
            flush=True,
        )
    return removed


def fetch_latest_q2rtx_release_metadata() -> tuple[str, str, str]:
    """Return the latest PenguinBurner Q2RTX benchmark release metadata."""
    release = _github_json(PB_Q2RTX_LATEST_RELEASE_API_URL)
    tag_name = str(release.get("tag_name", "")).strip()
    if not tag_name:
        raise StabilityTestError("latest Q2RTX release did not include a tag name")
    if bool(release.get("draft")) or bool(release.get("prerelease")):
        raise StabilityTestError(f"latest Q2RTX release {tag_name} is not final")

    expected_asset_name = f"{PB_Q2RTX_ASSET_PREFIX}{tag_name}{PB_Q2RTX_ASSET_SUFFIX}"
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise StabilityTestError(f"latest Q2RTX release {tag_name} has no assets")

    fallback_asset: tuple[str, str] | None = None
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        asset_name = str(asset.get("name", "")).strip()
        asset_url = str(asset.get("browser_download_url", "")).strip()
        if not asset_name or not asset_url:
            continue
        if asset_name == expected_asset_name:
            return tag_name, asset_name, asset_url
        if (
            fallback_asset is None
            and asset_name.startswith(PB_Q2RTX_ASSET_PREFIX)
            and asset_name.endswith(PB_Q2RTX_ASSET_SUFFIX)
        ):
            fallback_asset = (asset_name, asset_url)

    if fallback_asset is not None:
        asset_name, asset_url = fallback_asset
        return tag_name, asset_name, asset_url

    raise StabilityTestError(
        f"latest Q2RTX release {tag_name} has no Linux x86_64 PenguinBurner asset"
    )


def _q2rtx_release_asset_urls(
    tag_name: str,
    asset_name: str,
    primary_url: str | None = None,
) -> tuple[str, ...]:
    return (
        primary_url
        or f"https://github.com/{PB_Q2RTX_REPOSITORY}/releases/download/{tag_name}/{asset_name}",
    )


def _copy_existing_q2rtx_data(install_dir: Path) -> bool:
    install_dir = install_dir.expanduser().resolve()
    for root in _default_q2rtx_roots():
        root = root.expanduser().resolve()
        if root == install_dir:
            continue
        if not all((root / relative).is_file() for relative in Q2RTX_REQUIRED_DATA_FILES):
            continue
        destination_baseq2 = install_dir / "baseq2"
        destination_baseq2.mkdir(parents=True, exist_ok=True)
        for relative in Q2RTX_REQUIRED_DATA_FILES:
            source = root / relative
            destination = install_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        claim_desktop_user_ownership(destination_baseq2, recursive=True)
        return True
    return False


def _ensure_q2rtx_shareware_data(
    *,
    install_dir: Path,
    cache_dir: Path,
    show_progress: bool,
    progress_callback: DependencyProgressCallback | None,
) -> str:
    if all((install_dir / relative).is_file() for relative in Q2RTX_REQUIRED_DATA_FILES):
        return "existing"

    if _copy_existing_q2rtx_data(install_dir):
        return "local"

    data_archive_path = cache_dir / Q2RTX_SHAREWARE_DATA_ASSET_NAME
    if not data_archive_path.is_file():
        _download_file_from_urls(
            (Q2RTX_SHAREWARE_DATA_ASSET_URL,),
            data_archive_path,
            label=f"Q2RTX {Q2RTX_SHAREWARE_DATA_VERSION}",
            show_progress=show_progress,
            progress_callback=progress_callback,
            progress_start_pct=72.0,
            progress_end_pct=94.0,
        )
    elif show_progress:
        print(
            f"Using cached Q2RTX shareware data archive {data_archive_path}",
            flush=True,
        )

    _emit_dependency_progress(
        progress_callback,
        95.0,
        "Extracting Q2RTX shareware data",
        path=str(install_dir),
    )
    _extract_q2rtx_data_files(
        data_archive_path,
        install_dir,
        required_files=Q2RTX_REQUIRED_DATA_FILES,
    )
    return "downloaded"


def install_latest_q2rtx(
    *,
    data_dir: Path | None = None,
    cache_dir: Path | None = None,
    show_progress: bool = True,
    progress_callback: DependencyProgressCallback | None = None,
) -> Q2RTXInstallResult:
    if show_progress:
        print("Q2RTX install: resolving latest PenguinBurner headless release...", flush=True)
    _emit_dependency_progress(
        progress_callback,
        2.0,
        "Fetching Q2RTX release metadata",
    )
    tag_name, asset_name, asset_url = fetch_latest_q2rtx_release_metadata()
    version = tag_name

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
    removed_log_count = clear_q2rtx_stability_logs(show_progress=show_progress)
    if removed_log_count:
        _emit_dependency_progress(
            progress_callback,
            6.0,
            "Removed old Q2RTX stability logs",
            count=removed_log_count,
            path=str(DEFAULT_LOG_DIR),
        )
    archive_path = resolved_cache_dir / asset_name
    install_dir = resolved_data_dir / version
    if show_progress:
        print(
            f"Q2RTX install: version={version} asset={asset_name}",
            flush=True,
        )
    _emit_dependency_progress(
        progress_callback,
        8.0,
        f"Found Q2RTX {version}",
        version=version,
        asset=asset_name,
    )

    selected_asset_url = asset_url
    if not archive_path.is_file():
        selected_asset_url = _download_file_from_urls(
            _q2rtx_release_asset_urls(tag_name, asset_name, asset_url),
            archive_path,
            label=f"Q2RTX {version} Linux build",
            show_progress=show_progress,
            progress_callback=progress_callback,
            progress_start_pct=10.0,
            progress_end_pct=58.0,
        )
    elif show_progress:
        print(f"Using cached Q2RTX archive {archive_path}", flush=True)
    if archive_path.is_file():
        _emit_dependency_progress(
            progress_callback,
            58.0,
            "Q2RTX archive is available",
            path=str(archive_path),
        )
    if show_progress:
        print(f"Q2RTX install: extracting archive to {install_dir}...", flush=True)
    _emit_dependency_progress(
        progress_callback,
        62.0,
        "Extracting Q2RTX archive",
        path=str(install_dir),
    )
    _extract_q2rtx_archive(archive_path, install_dir)
    _emit_dependency_progress(
        progress_callback,
        70.0,
        "Q2RTX archive extracted",
        path=str(install_dir),
    )

    executable_path, workdir = resolve_q2rtx_executable(root=install_dir)
    if workdir != install_dir:
        install_dir = workdir

    if show_progress:
        print("Q2RTX install: preparing shareware demo data...", flush=True)
    _emit_dependency_progress(
        progress_callback,
        72.0,
        "Preparing Q2RTX shareware data",
    )
    data_source = _ensure_q2rtx_shareware_data(
        install_dir=install_dir,
        cache_dir=resolved_cache_dir,
        show_progress=show_progress,
        progress_callback=progress_callback,
    )
    if show_progress:
        print(f"Q2RTX install: shareware data source={data_source}", flush=True)
        print(
            f"Q2RTX install: checking runtime libraries for {executable_path}...",
            flush=True,
        )
    _prepare_q2rtx_runtime_env(
        executable_path,
        show_progress=show_progress,
        progress_callback=progress_callback,
        progress_start_pct=96.0,
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
        asset_url=selected_asset_url,
        archive_path=archive_path,
        install_dir=install_dir,
        executable_path=executable_path,
    )
