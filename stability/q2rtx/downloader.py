from __future__ import annotations

import json
from pathlib import Path
import time
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from penguin_burner_paths import claim_desktop_user_ownership

from .models import StabilityTestError
from .progress import (
    DependencyProgressCallback,
    _emit_dependency_progress,
    _progress_range_value,
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


def _unique_https_urls(urls: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    unique: list[str] = []
    for url in urls:
        clean_url = _require_https_url(str(url).strip())
        if clean_url not in unique:
            unique.append(clean_url)
    return tuple(unique)


def _join_mirror_url(base_url: str, filename: str) -> str:
    base = _require_https_url(base_url)
    if not base.endswith("/"):
        base += "/"
    return _require_https_url(urllib_parse.urljoin(base, filename))


def _format_attempt_errors(errors: list[tuple[str, str]]) -> str:
    return "; ".join(f"{url}: {detail}" for url, detail in errors)


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
            raise StabilityTestError(
                f"download failed from {url} ({exc.code}): {detail}"
            ) from exc
        raise StabilityTestError(
            f"download failed from {url} with status {exc.code}"
        ) from exc
    except urllib_error.URLError as exc:
        raise StabilityTestError(f"download failed from {url}: {exc}") from exc
    except TimeoutError as exc:
        raise StabilityTestError(
            f"download timed out while reading {url}: {exc}"
        ) from exc
    except OSError as exc:
        raise StabilityTestError(f"download failed from {url}: {exc}") from exc


def _download_text_from_urls(
    urls: tuple[str, ...],
    *,
    label: str,
) -> tuple[str, str]:
    errors: list[tuple[str, str]] = []
    for url in _unique_https_urls(urls):
        try:
            return url, _download_text(url)
        except StabilityTestError as exc:
            errors.append((url, str(exc)))
    raise StabilityTestError(
        f"failed to fetch {label}; tried {_format_attempt_errors(errors)}"
    )


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
                f"GitHub API request failed for {url} ({exc.code}): {detail}"
            ) from exc
        raise StabilityTestError(
            f"GitHub API request failed for {url} with status {exc.code}"
        ) from exc
    except urllib_error.URLError as exc:
        raise StabilityTestError(f"failed to contact GitHub at {url}: {exc}") from exc
    except TimeoutError as exc:
        raise StabilityTestError(
            f"GitHub API request timed out while reading {url}: {exc}"
        ) from exc
    except OSError as exc:
        raise StabilityTestError(f"GitHub API request failed for {url}: {exc}") from exc

    try:
        data = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise StabilityTestError("GitHub API returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise StabilityTestError("GitHub API returned an unexpected payload")
    return data


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
            raise StabilityTestError(
                f"download failed from {url} ({exc.code}): {detail}"
            ) from exc
        raise StabilityTestError(
            f"download failed from {url} with status {exc.code}"
        ) from exc
    except urllib_error.URLError as exc:
        raise StabilityTestError(f"download failed from {url}: {exc}") from exc
    except TimeoutError as exc:
        raise StabilityTestError(
            f"download timed out while reading {url}: {exc}"
        ) from exc
    except OSError as exc:
        raise StabilityTestError(
            f"failed writing download from {url} to {tmp_path}: {exc}"
        ) from exc

    try:
        tmp_path.replace(destination)
    except OSError as exc:
        raise StabilityTestError(
            f"failed to finalize download at {destination}: {exc}"
        ) from exc
    claim_desktop_user_ownership(destination)


def _download_file_from_urls(
    urls: tuple[str, ...],
    destination: Path,
    *,
    label: str,
    show_progress: bool,
    progress_callback: DependencyProgressCallback | None = None,
    progress_start_pct: float = 0.0,
    progress_end_pct: float = 100.0,
) -> str:
    errors: list[tuple[str, str]] = []
    source_urls = _unique_https_urls(urls)
    for index, url in enumerate(source_urls):
        try:
            _download_file(
                url,
                destination,
                label=label,
                show_progress=show_progress,
                progress_callback=progress_callback,
                progress_start_pct=progress_start_pct,
                progress_end_pct=progress_end_pct,
            )
            return url
        except StabilityTestError as exc:
            errors.append((url, str(exc)))
            has_next_source = index < len(source_urls) - 1
            detail = (
                f"{label} download failed; trying mirror"
                if has_next_source
                else f"{label} download failed; no mirrors left"
            )
            _emit_dependency_progress(
                progress_callback,
                progress_start_pct,
                detail,
                label=label,
                url=url,
                error=str(exc),
            )
            if show_progress:
                suffix = "trying next mirror" if has_next_source else "no mirrors left"
                print(
                    f"Download failed from {url}; {suffix}: {exc}",
                    flush=True,
                )
    raise StabilityTestError(
        f"failed to download {label}; tried {_format_attempt_errors(errors)}"
    )
