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
NVAPI_SHIM_SOURCE_DIR = ROOT / "overlay" / "native" / "nvapi_shim"
NVAPI_SHIM_DLL = "nvapi64.dll"
NVAPI_SHIM_COMPILER = "x86_64-w64-mingw32-g++"
DAEMON_SOURCE_DIR = ROOT / "burnerd"
DAEMON_MANIFEST = DAEMON_SOURCE_DIR / "Cargo.toml"
DAEMON_BINARY = "penguin-burnerd"

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
        self._build_nvapi_shim()
        self._build_rust_daemon()

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

    def _build_nvapi_shim(self) -> None:
        # Cross-compile the drop-in NVAPI latency shim (a PE nvapi64.dll) with
        # MinGW and stage it inside the package. Optional: when MinGW is absent
        # the in-game latency path simply falls back to dxvk-nvapi marker-log /
        # trace, so a missing toolchain is a warning, not a failure.
        if _env_flag_disabled("PENGUIN_BURNER_BUILD_NVAPI_SHIM"):
            return
        require_shim = _env_flag_enabled("PENGUIN_BURNER_REQUIRE_NVAPI_SHIM")
        source = NVAPI_SHIM_SOURCE_DIR / "src" / "nvapi_shim.cpp"
        def_file = NVAPI_SHIM_SOURCE_DIR / NVAPI_SHIM_DLL.replace(".dll", ".def")
        if not source.exists() or not def_file.exists():
            return
        compiler = shutil.which(NVAPI_SHIM_COMPILER)
        if not compiler:
            self._nvapi_shim_unavailable(
                f"{NVAPI_SHIM_COMPILER} (MinGW) is required to build the NVAPI shim",
                require=require_shim,
            )
            return
        output_dir = Path(self.build_lib) / "overlay" / "nvapi_shim"
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / NVAPI_SHIM_DLL
        try:
            subprocess.check_call(
                [
                    compiler,
                    "-O2",
                    "-shared",
                    "-std=c++17",
                    "-fno-exceptions",
                    "-fno-rtti",
                    "-static",
                    "-static-libgcc",
                    "-static-libstdc++",
                    "-o",
                    str(output),
                    str(source),
                    str(def_file),
                    "-lkernel32",
                ]
            )
        except subprocess.CalledProcessError as exc:
            if output.exists():
                output.unlink()
            self._nvapi_shim_unavailable(
                f"NVAPI shim build failed: {exc}",
                require=require_shim,
            )
            return
        if not output.exists():
            self._nvapi_shim_unavailable(
                f"NVAPI shim build did not produce {output}",
                require=require_shim,
            )

    def _nvapi_shim_unavailable(self, message: str, *, require: bool) -> None:
        if require:
            raise RuntimeError(message)
        self.warn(f"{message}; installing Python package without the NVAPI latency shim")

    def _build_rust_daemon(self) -> None:
        # Compile the root daemon (burnerd/ Rust crate) and stage the release
        # binary inside the package at runtime/daemon_bin/penguin-burnerd so the
        # elevated systemd-install step has a copy to place at
        # /usr/libexec/penguin-burnerd. The unit always executes that root-owned
        # target; the bundled file is only an install source. Without cargo, a
        # plain `pip install .` still yields a working GUI/CLI, so a missing
        # toolchain is a warning unless REQUIRE_DAEMON forces it (release/native
        # builds do).
        if _env_flag_disabled("PENGUIN_BURNER_BUILD_DAEMON"):
            return
        require_daemon = _env_flag_enabled("PENGUIN_BURNER_REQUIRE_DAEMON")
        if not _native_layer_supported():
            self._daemon_unavailable(
                "penguin-burnerd is only built for Linux",
                require=require_daemon,
            )
            return
        if not DAEMON_MANIFEST.exists():
            return
        cargo = shutil.which("cargo")
        if not cargo:
            self._daemon_unavailable(
                "cargo (Rust toolchain) is required to build the penguin-burnerd daemon",
                require=require_daemon,
            )
            return
        output_dir = Path(self.build_lib) / "runtime" / "daemon_bin"
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            # --locked pins the committed burnerd/Cargo.lock so the wheel builds
            # the exact dependency set the native packages compile.
            subprocess.check_call(
                [
                    cargo,
                    "build",
                    "--release",
                    "--locked",
                    "--manifest-path",
                    str(DAEMON_MANIFEST),
                ]
            )
        except subprocess.CalledProcessError as exc:
            self._daemon_unavailable(
                f"penguin-burnerd daemon build failed: {exc}",
                require=require_daemon,
            )
            return
        source = DAEMON_SOURCE_DIR / "target" / "release" / DAEMON_BINARY
        if not source.exists():
            self._daemon_unavailable(
                f"penguin-burnerd daemon build did not produce {source}",
                require=require_daemon,
            )
            return
        destination = output_dir / DAEMON_BINARY
        shutil.copy2(source, destination)
        os.chmod(destination, 0o755)

    def _daemon_unavailable(self, message: str, *, require: bool) -> None:
        if require:
            raise RuntimeError(message)
        self.warn(f"{message}; installing Python package without the penguin-burnerd daemon")


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
