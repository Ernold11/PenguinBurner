from __future__ import annotations

import os
from pathlib import Path
import platform
import re
import shutil
import subprocess

from setuptools import setup
from setuptools.dist import Distribution
from setuptools.command.build_py import build_py as _build_py
from wheel.bdist_wheel import bdist_wheel as _bdist_wheel


ROOT = Path(__file__).resolve().parent
NATIVE_LAYER_SOURCE_DIR = ROOT / "overlay" / "native" / "latency_layer"
NATIVE_LAYER_LIBRARY = "libVkLayer_penguinburner_latency.so"
NATIVE_LAYER_MANIFEST = "VkLayer_PENGUINBURNER_latency.json"

# README uses repo-relative image/link paths so it renders on GitHub and in the
# local docs preview. PyPI does not resolve relative paths, so rewrite them to
# absolute GitHub URLs for the published long_description only.
_REPO_RAW = "https://raw.githubusercontent.com/jpietek/PenguinBurner/main/"
_REPO_BLOB = "https://github.com/jpietek/PenguinBurner/blob/main/"


def _absolutize_readme(text: str) -> str:
    def _is_relative(target: str) -> bool:
        return not target.startswith(("http://", "https://", "#", "mailto:"))

    # HTML <img src="..."> (logo, badges-as-html): only relative ones.
    text = re.sub(
        r'(<img\b[^>]*?\bsrc=")([^"]+)(")',
        lambda m: m.group(1)
        + (_REPO_RAW + m.group(2) if _is_relative(m.group(2)) else m.group(2))
        + m.group(3),
        text,
    )
    # Markdown images ![alt](path) -> raw URL.
    text = re.sub(
        r"(!\[[^\]]*\]\()([^)]+)(\))",
        lambda m: m.group(1)
        + (_REPO_RAW + m.group(2) if _is_relative(m.group(2)) else m.group(2))
        + m.group(3),
        text,
    )
    # Markdown links [text](path) -> blob URL (skip images, handled above).
    text = re.sub(
        r"(?<!!)(\[[^\]]*\]\()([^)]+)(\))",
        lambda m: m.group(1)
        + (_REPO_BLOB + m.group(2) if _is_relative(m.group(2)) else m.group(2))
        + m.group(3),
        text,
    )
    return text


def _long_description() -> str:
    return _absolutize_readme((ROOT / "README.md").read_text(encoding="utf-8"))


class build_py(_build_py):
    def run(self) -> None:
        super().run()
        self._build_native_latency_layer()

    def _build_native_latency_layer(self) -> None:
        if _env_flag_disabled("PENGUIN_BURNER_BUILD_NATIVE_LAYER"):
            return
        require_native = _env_flag_enabled("PENGUIN_BURNER_REQUIRE_NATIVE_LAYER")
        if not _native_layer_supported():
            self._native_layer_unavailable(
                "native Vulkan layer is only built for Linux x86_64",
                require=require_native,
            )
            return
        if not NATIVE_LAYER_SOURCE_DIR.exists():
            return
        cmake = shutil.which("cmake")
        if not cmake:
            self._native_layer_unavailable(
                "cmake is required to build the PenguinBurner native Vulkan layer",
                require=require_native,
            )
            return
        build_root = Path(self.build_lib).parent / "penguinburner-latency-layer"
        if build_root.exists():
            shutil.rmtree(build_root)
        output_dir = Path(self.build_lib) / "overlay" / "native_layer"
        output_dir.mkdir(parents=True, exist_ok=True)

        try:
            subprocess.check_call(
                [
                    cmake,
                    "-S",
                    str(NATIVE_LAYER_SOURCE_DIR),
                    "-B",
                    str(build_root),
                    "-DCMAKE_BUILD_TYPE=Release",
                ]
            )
            subprocess.check_call(
                [cmake, "--build", str(build_root), "--config", "Release"]
            )
        except subprocess.CalledProcessError as exc:
            self._native_layer_unavailable(
                f"native Vulkan layer build failed: {exc}",
                require=require_native,
            )
            return
        for name in (NATIVE_LAYER_LIBRARY, NATIVE_LAYER_MANIFEST):
            source = build_root / name
            if not source.exists():
                self._native_layer_unavailable(
                    f"native Vulkan layer build did not produce {source}",
                    require=require_native,
                )
                return
            shutil.copy2(source, output_dir / name)

    def _native_layer_unavailable(self, message: str, *, require: bool) -> None:
        if require:
            raise RuntimeError(message)
        self.warn(f"{message}; installing Python package without native overlay layer")


class bdist_wheel(_bdist_wheel):
    def finalize_options(self) -> None:
        super().finalize_options()
        self.root_is_pure = False

    def get_tag(self) -> tuple[str, str, str]:
        _python, _abi, platform_tag = super().get_tag()
        return "py3", "none", platform_tag


class BinaryDistribution(Distribution):
    def has_ext_modules(self) -> bool:
        return True


def _env_flag_disabled(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }


def _env_flag_enabled(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _native_layer_supported() -> bool:
    return platform.system().lower() == "linux"


setup(
    cmdclass={"build_py": build_py, "bdist_wheel": bdist_wheel},
    distclass=BinaryDistribution,
    long_description=_long_description(),
    long_description_content_type="text/markdown",
)
