from __future__ import annotations

from pathlib import Path
import re
from urllib import parse as urllib_parse

from common.penguin_burner_paths import claim_desktop_user_ownership

from .archive_extraction import _extract_q2rtx_archive
from .assets import resolve_q2rtx_executable
from .constants import (
    Q2RTX_GITHUB_RELEASES_URL,
    Q2RTX_RELEASE_DOWNLOAD_BASE_URLS,
    Q2RTX_RELEASES_API_URL,
    Q2RTX_RELEASES_LATEST_URL,
)
from .downloader import (
    _download_file_from_urls,
    _download_text_from_urls,
    _format_attempt_errors,
    _github_json,
    _join_mirror_url,
    _require_https_url,
    _unique_https_urls,
)
from .models import Q2RTXInstallResult, StabilityTestError
from .paths import (
    default_q2rtx_compat_dir,
    default_q2rtx_install_cache_dir,
    default_q2rtx_install_data_dir,
)
from .progress import DependencyProgressCallback, _emit_dependency_progress
from .runtime_env import _prepare_q2rtx_runtime_env

# Re-exported for backward compatibility: the download/archive/OpenSSL/runtime-env
# helpers now live in sibling modules. ``default_q2rtx_compat_dir`` is surfaced
# here (and via ``__all__``) because ``stability.q2rtx`` re-exports it.
__all__ = [
    "default_q2rtx_compat_dir",
    "default_q2rtx_install_cache_dir",
    "default_q2rtx_install_data_dir",
    "fetch_latest_q2rtx_release_metadata",
    "install_latest_q2rtx",
]


def _q2rtx_release_asset_urls(
    tag_name: str,
    asset_name: str,
    primary_url: str | None = None,
) -> tuple[str, ...]:
    urls: list[str] = []
    if primary_url:
        urls.append(primary_url)
    tag_path = urllib_parse.quote(tag_name.strip(), safe="")
    asset_path = urllib_parse.quote(asset_name.strip(), safe="")
    for base_url in Q2RTX_RELEASE_DOWNLOAD_BASE_URLS:
        urls.append(_join_mirror_url(base_url, f"{tag_path}/{asset_path}"))
    return _unique_https_urls(tuple(urls))


def _fetch_latest_q2rtx_release_metadata_from_api() -> tuple[str, str, str]:
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


def _fetch_latest_q2rtx_release_metadata_from_page() -> tuple[str, str, str]:
    _source_url, text = _download_text_from_urls(
        (Q2RTX_RELEASES_LATEST_URL, Q2RTX_GITHUB_RELEASES_URL),
        label="Q2RTX release page",
    )
    tag_matches = re.findall(
        r"/NVIDIA/Q2RTX/releases/tag/(?P<tag>v[0-9][A-Za-z0-9_.-]*)",
        text,
    )
    if not tag_matches:
        tag_matches = re.findall(r"\b(?P<tag>v[0-9]+\.[0-9]+(?:\.[0-9]+)?)\b", text)
    if not tag_matches:
        raise StabilityTestError(
            f"could not find a Q2RTX release tag in {Q2RTX_GITHUB_RELEASES_URL}"
        )
    tag_name = tag_matches[0]
    version = tag_name[1:] if tag_name.startswith("v") else tag_name
    asset_name = f"q2rtx-{version}-linux.tar.gz"
    asset_url = _q2rtx_release_asset_urls(tag_name, asset_name)[0]
    return tag_name, asset_name, asset_url


def fetch_latest_q2rtx_release_metadata() -> tuple[str, str, str]:
    errors: list[tuple[str, str]] = []
    try:
        return _fetch_latest_q2rtx_release_metadata_from_api()
    except StabilityTestError as exc:
        errors.append((Q2RTX_RELEASES_API_URL, str(exc)))

    try:
        return _fetch_latest_q2rtx_release_metadata_from_page()
    except StabilityTestError as exc:
        errors.append((Q2RTX_RELEASES_LATEST_URL, str(exc)))

    raise StabilityTestError(
        "failed to fetch latest Q2RTX release metadata; tried "
        + _format_attempt_errors(errors)
    )


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

    asset_urls = _q2rtx_release_asset_urls(tag_name, asset_name, asset_url)
    selected_asset_url = asset_url
    if not archive_path.is_file():
        selected_asset_url = _download_file_from_urls(
            asset_urls,
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
        asset_url=selected_asset_url,
        archive_path=archive_path,
        install_dir=install_dir,
        executable_path=executable_path,
    )
