from __future__ import annotations

import overlay.native_layer as native_layer
from overlay.native_layer import NATIVE_LAYER_DIR_ENV
from overlay.native_layer import NATIVE_LAYER_LIBRARY
from overlay.native_layer import NATIVE_LAYER_MANIFEST
from overlay.native_layer import native_layer_dirs


def test_native_layer_dirs_prefers_explicit_installed_layer(tmp_path) -> None:
    explicit = _fake_native_layer_dir(tmp_path / "installed")
    source = _fake_native_layer_dir(tmp_path / "source")

    assert native_layer_dirs(
        {NATIVE_LAYER_DIR_ENV: str(explicit)},
        source_build_dir=source,
    ) == [explicit, source]


def test_native_layer_dirs_ignores_incomplete_layer_dirs(tmp_path) -> None:
    explicit = tmp_path / "installed"
    explicit.mkdir()
    source = _fake_native_layer_dir(tmp_path / "source")

    assert native_layer_dirs(
        {NATIVE_LAYER_DIR_ENV: str(explicit)},
        source_build_dir=source,
    ) == [source]


def test_native_layer_dirs_only_uses_source_fallback_in_source_checkout(
    monkeypatch, tmp_path
) -> None:
    source = _fake_native_layer_dir(
        tmp_path / "overlay" / "native" / "latency_layer" / "build"
    )
    monkeypatch.setattr(native_layer, "_SOURCE_PROJECT_FILE", tmp_path / "pyproject.toml")
    monkeypatch.setattr(
        native_layer,
        "_SOURCE_NATIVE_LAYER_DIR",
        tmp_path / "overlay" / "native" / "latency_layer",
    )
    monkeypatch.setattr(native_layer, "_SOURCE_BUILD_LAYER_DIR", source)

    assert native_layer_dirs({}) == []

    (
        tmp_path / "overlay" / "native" / "latency_layer" / "CMakeLists.txt"
    ).write_text("", encoding="utf-8")

    assert native_layer_dirs({}) == []

    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    (
        tmp_path / "overlay" / "native" / "latency_layer" / "CMakeLists.txt"
    ).write_text("", encoding="utf-8")

    assert native_layer_dirs({}) == [source]


def _fake_native_layer_dir(path):
    path.mkdir(parents=True)
    (path / NATIVE_LAYER_MANIFEST).write_text("{}", encoding="utf-8")
    (path / NATIVE_LAYER_LIBRARY).write_bytes(b"")
    return path
