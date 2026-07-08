from __future__ import annotations

import json
import os
import subprocess
import struct
import tomllib
import zlib
from pathlib import Path


def test_base_package_installs_gui_dependencies() -> None:
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    dependencies = set(metadata["project"]["dependencies"])

    assert "PySide6-Essentials>=6.7" in dependencies
    assert "colorama>=0.4" in dependencies
    assert "pyqtgraph>=0.13" in dependencies


def test_ui_extra_remains_as_compatibility_alias() -> None:
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    ui_dependencies = set(metadata["project"]["optional-dependencies"]["ui"])

    assert "PySide6-Essentials>=6.7" in ui_dependencies
    assert "colorama>=0.4" in ui_dependencies
    assert "pyqtgraph>=0.13" in ui_dependencies


def test_native_packages_install_pyqtgraph_colorama_runtime_dependency() -> None:
    arch_pkgbuild = Path("packaging/arch/PKGBUILD").read_text(encoding="utf-8")
    debian_control = Path("packaging/debian/control").read_text(encoding="utf-8")
    rpm_spec = Path("packaging/rpm/penguin-burner.spec").read_text(encoding="utf-8")

    assert "'python-colorama>=0.4'" in arch_pkgbuild
    assert "'python-pyqtgraph>=0.13'" in arch_pkgbuild
    assert " python3-colorama (>= 0.4)," in debian_control
    assert " python3-pyqtgraph (>= 0.13)," in debian_control
    assert "Requires:       python3-colorama >= 0.4" in rpm_spec
    assert "Requires:       python3-pyqtgraph >= 0.13" in rpm_spec


def test_native_packages_install_pyside6_runtime_dependency() -> None:
    arch_pkgbuild = Path("packaging/arch/PKGBUILD").read_text(encoding="utf-8")
    debian_control = Path("packaging/debian/control").read_text(encoding="utf-8")
    rpm_spec = Path("packaging/rpm/penguin-burner.spec").read_text(encoding="utf-8")

    assert "'pyside6>=6.7'" in arch_pkgbuild
    assert " python3-pyside6.qtcore (>= 6.7)," in debian_control
    assert " python3-pyside6.qtgui (>= 6.7)," in debian_control
    assert " python3-pyside6.qtwidgets (>= 6.7)," in debian_control
    assert "Requires:       python3-pyside6 >= 6.7" in rpm_spec


def test_native_package_versions_match_python_project() -> None:
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    version = metadata["project"]["version"]
    arch_pkgbuild = Path("packaging/arch/PKGBUILD").read_text(encoding="utf-8")
    rpm_spec = Path("packaging/rpm/penguin-burner.spec").read_text(encoding="utf-8")

    assert f"pkgver={version}" in arch_pkgbuild
    assert f"Version:        {version}" in rpm_spec


def test_console_scripts_use_gui_default_and_explicit_cli_names() -> None:
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    scripts = metadata["project"]["scripts"]
    rpm_spec = Path("packaging/rpm/penguin-burner.spec").read_text(
        encoding="utf-8"
    )

    assert scripts["penguin-burner"] == "ui.main:main"
    assert scripts["pburn"] == "ui.main:main"
    assert scripts["penguin-burner-ui"] == "ui.main:main"
    assert scripts["pburn-ui"] == "ui.main:main"
    assert "penguin-burner-yolo" not in scripts
    assert "pburn-yolo" not in scripts
    assert scripts["penguin-burner-cli"] == "penguin_burner:cli_main"
    assert scripts["pburn-cli"] == "penguin_burner:cli_main"
    assert "penguin-burner-steam-launch-check" not in scripts
    assert "penguin-burner-steam-game-setup" not in scripts
    assert scripts["PENGUIN_BURNER"] == "overlay.launcher:main"
    assert (
        scripts["penguin-burner-install-wrappers"]
        == "common.flatpak_wrappers:main"
    )
    assert "%{_bindir}/penguin-burner-install-wrappers" in rpm_spec
    assert "PB_OVERLAY" not in scripts
    assert "penguin-burner-overlay" not in scripts
    assert "pburn-overlay" not in scripts
    assert "pb-overlay" not in scripts
    assert "penguin-burner-overlay-text" not in scripts
    assert "pburn-overlay-text" not in scripts
    assert "pb-overlay-text" not in scripts
    assert "q2rtx-stability" not in scripts
    assert "cuda-bruteforce-stability" not in scripts
    assert "penguin-burner-import-afterburner-vf" not in scripts
    assert "penguin-burner-import-afterburner-fan" not in scripts
    assert "penguin_burner" not in scripts


def test_package_installs_desktop_launcher_and_icons() -> None:
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    data_files = metadata["tool"]["setuptools"]["data-files"]
    package_data = metadata["tool"]["setuptools"]["package-data"]
    packages = set(metadata["tool"]["setuptools"]["packages"])

    assert "ui.assets" in packages
    assert "native_layer/*.json" in package_data["overlay"]
    assert "native_layer/*.so" in package_data["overlay"]
    assert data_files["share/applications"] == [
        "packaging/linux/io.github.jpietek.PenguinBurner.desktop"
    ]
    assert data_files["share/icons/hicolor/256x256/apps"] == [
        "packaging/icons/hicolor/256x256/apps/penguin-burner.png"
    ]
    assert data_files["share/icons/hicolor/512x512/apps"] == [
        "packaging/icons/hicolor/512x512/apps/penguin-burner.png"
    ]
    assert "PenguinBurner.service" not in "\n".join(
        item for values in data_files.values() for item in values
    )
    assert "*.png" in package_data["ui.assets"]


def test_flatpak_manifest_exposes_daemon_socket_and_host_spawn_portal() -> None:
    manifest = Path("packaging/flatpak/io.github.jpietek.PenguinBurner.yml").read_text(
        encoding="utf-8"
    )

    assert "--filesystem=/run/penguin-burnerd.sock" in manifest
    assert "--talk-name=org.freedesktop.Flatpak" in manifest
    assert "PENGUIN_BURNER_REQUIRE_NATIVE_LAYER" in manifest
    assert "PENGUIN_BURNER_BUILD_NATIVE_LAYER: \"0\"" not in manifest


def test_flatpak_smoke_script_uses_isolated_profile_and_app_id_launcher() -> None:
    script = Path("scripts/check-flatpak-install-smoke.sh").read_text(
        encoding="utf-8"
    )

    assert "XDG_DATA_HOME" in script
    assert 'export PATH="/usr/bin:/bin"' in script
    assert "host command leaked into smoke test PATH" in script
    assert "exports/bin/$APP_ID" in script
    assert 'flatpak run --user --command=bash "$APP_ID" -c' in script
    assert 'test "$resolved" = "/app/bin/$bin"' in script
    assert 'flatpak run --user --command=penguin-burner-cli "$APP_ID" --help' in script
    assert 'flatpak run --user --command=pburn-cli "$APP_ID" --help' in script
    assert "penguin-burner-install-wrappers" in script


def test_flatpak_cli_wrapper_installer_is_conservative() -> None:
    script = Path("common/flatpak_wrappers.py").read_text(
        encoding="utf-8"
    )
    build_script = Path("scripts/build-flatpak-public-repo.sh").read_text(
        encoding="utf-8"
    )

    assert "penguin-burner" in script
    assert "pburn" in script
    assert "penguin-burner-cli" in script
    assert "pburn-cli" in script
    assert "PENGUIN_BURNER" in script
    assert "PENGUIN_BURNER_Q2RTX_USER" in script
    assert "exec /usr/bin/flatpak run --user --command={command_name} {APP_ID}" in script
    assert "refusing to overwrite existing command" in script
    assert "--force" in script
    assert "--uninstall" in script
    assert "penguin-burner-install-wrappers" in build_script
    assert "install -m 0644" not in build_script
    assert "$ROOT/scripts/install-flatpak-cli-wrappers.sh" not in build_script


def test_flatpak_cli_wrappers_forward_to_flatpak_but_steam_wrapper_does_not() -> None:
    from common.flatpak_wrappers import _wrapper_text

    cli_wrapper = _wrapper_text("pburn-cli")
    steam_wrapper = _wrapper_text("PENGUIN_BURNER")

    assert "exec /usr/bin/flatpak run --user --command=pburn-cli" in cli_wrapper
    assert "flatpak run" not in steam_wrapper
    assert 'exec "$@"' in steam_wrapper
    assert "VK_ADD_IMPLICIT_LAYER_PATH" in steam_wrapper
    assert "PENGUIN_BURNER_NATIVE_LAYER_DIR" in steam_wrapper


def test_flatpak_steam_wrapper_launches_game_on_host_when_layer_missing(tmp_path) -> None:
    from common.flatpak_wrappers import _wrapper_text

    wrapper = tmp_path / "PENGUIN_BURNER"
    env_file = tmp_path / "env.txt"
    game = tmp_path / "game"
    wrapper.write_text(_wrapper_text("PENGUIN_BURNER"), encoding="utf-8")
    wrapper.chmod(0o755)
    game.write_text(
        "#!/usr/bin/env sh\n"
        f"env | sort > {env_file}\n",
        encoding="utf-8",
    )
    game.chmod(0o755)

    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "PB_OVERLAY": "1",
        "PB_INGAME_LATENCY": "1",
        "PENGUIN_BURNER_FLATPAK_APP_PATH": str(tmp_path / "missing-app-files"),
    }
    result = subprocess.run(
        [str(wrapper), str(game)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    captured = env_file.read_text(encoding="utf-8")
    assert "PENGUIN_BURNER=1\n" in captured
    assert "PENGUIN_BURNER_OVERLAY=1\n" in captured
    assert "PENGUIN_BURNER_LATENCY_LAYER=1\n" in captured
    assert "PENGUIN_BURNER_LATENCY_DISPLAY=1\n" in captured
    assert "VK_ADD_IMPLICIT_LAYER_PATH=" not in captured


def test_flatpak_steam_wrapper_routes_ingame_latency_stderr_to_fifo(tmp_path) -> None:
    from common.flatpak_wrappers import _wrapper_text

    wrapper = tmp_path / "PENGUIN_BURNER"
    fd_file = tmp_path / "fd2.txt"
    game = tmp_path / "game"
    cache_home = tmp_path / "cache"
    trace_fifo = cache_home / "penguin-burner" / "nvapi-trace.fifo"
    wrapper.write_text(_wrapper_text("PENGUIN_BURNER"), encoding="utf-8")
    wrapper.chmod(0o755)
    game.write_text(
        "#!/usr/bin/env sh\n"
        f"test -p {trace_fifo}\n"
        f"readlink /proc/$$/fd/2 > {fd_file}\n"
        "printf 'nvapi marker line\\n' >&2\n",
        encoding="utf-8",
    )
    game.chmod(0o755)

    result = subprocess.run(
        [str(wrapper), str(game)],
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "XDG_CACHE_HOME": str(cache_home),
            "PB_INGAME_LATENCY": "1",
            "PENGUIN_BURNER_FLATPAK_APP_PATH": str(tmp_path / "missing-app-files"),
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert trace_fifo.is_fifo()
    assert fd_file.read_text(encoding="utf-8").strip() == str(trace_fifo)


def test_flatpak_steam_wrapper_falls_back_to_trace_with_unpatched_nvapi(
    tmp_path,
) -> None:
    from common.flatpak_wrappers import _wrapper_text

    compat_data = tmp_path / "compatdata" / "game"
    nvapi = compat_data / "pfx" / "drive_c" / "windows" / "system32" / "nvapi64.dll"
    nvapi.parent.mkdir(parents=True)
    nvapi.write_bytes(b"stock dxvk-nvapi")
    wrapper = tmp_path / "PENGUIN_BURNER"
    env_file = tmp_path / "env.txt"
    game = tmp_path / "game"
    wrapper.write_text(_wrapper_text("PENGUIN_BURNER"), encoding="utf-8")
    wrapper.chmod(0o755)
    game.write_text(
        "#!/usr/bin/env sh\n"
        f"env | sort > {env_file}\n",
        encoding="utf-8",
    )
    game.chmod(0o755)

    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"DXVK_NVAPI_LATENCY_MARKER_LOG", "DXVK_NVAPI_LOG_LEVEL"}
    }
    env.update(
        {
            "HOME": str(tmp_path),
            "PB_INGAME_LATENCY": "1",
            "STEAM_COMPAT_DATA_PATH": str(compat_data),
            "PENGUIN_BURNER_FLATPAK_APP_PATH": str(tmp_path / "missing-app-files"),
        }
    )
    result = subprocess.run(
        [str(wrapper), str(game)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    captured = env_file.read_text(encoding="utf-8")
    assert "DXVK_NVAPI_LOG_LEVEL=trace\n" in captured
    assert "DXVK_NVAPI_LATENCY_MARKER_LOG=" not in captured


def test_flatpak_steam_wrapper_prefers_marker_log_with_patched_nvapi(tmp_path) -> None:
    from common.flatpak_wrappers import _wrapper_text

    compat_data = tmp_path / "compatdata" / "game"
    nvapi = compat_data / "pfx" / "drive_c" / "windows" / "system32" / "nvapi64.dll"
    nvapi.parent.mkdir(parents=True)
    nvapi.write_bytes(b"patched DXVK_NVAPI_LATENCY_MARKER_LOG dxvk-nvapi")
    wrapper = tmp_path / "PENGUIN_BURNER"
    env_file = tmp_path / "env.txt"
    game = tmp_path / "game"
    wrapper.write_text(_wrapper_text("PENGUIN_BURNER"), encoding="utf-8")
    wrapper.chmod(0o755)
    game.write_text(
        "#!/usr/bin/env sh\n"
        f"env | sort > {env_file}\n",
        encoding="utf-8",
    )
    game.chmod(0o755)

    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"DXVK_NVAPI_LATENCY_MARKER_LOG", "DXVK_NVAPI_LOG_LEVEL"}
    }
    env.update(
        {
            "HOME": str(tmp_path),
            "PB_INGAME_LATENCY": "1",
            "STEAM_COMPAT_DATA_PATH": str(compat_data),
            "PENGUIN_BURNER_FLATPAK_APP_PATH": str(tmp_path / "missing-app-files"),
        }
    )
    result = subprocess.run(
        [str(wrapper), str(game)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    captured = env_file.read_text(encoding="utf-8")
    assert "DXVK_NVAPI_LATENCY_MARKER_LOG=1\n" in captured
    assert "DXVK_NVAPI_LOG_LEVEL=" not in captured


def test_flatpak_steam_wrapper_points_at_shipped_native_layer(tmp_path) -> None:
    from common.flatpak_wrappers import _wrapper_text

    app_files = tmp_path / "app" / "files"
    layer_dir = (
        app_files
        / "lib"
        / "python3.13"
        / "site-packages"
        / "overlay"
        / "native_layer"
    )
    layer_dir.mkdir(parents=True)
    layer_dir.joinpath("VkLayer_PENGUINBURNER_latency.json").write_text(
        "{}\n",
        encoding="utf-8",
    )
    layer_dir.joinpath("libVkLayer_penguinburner_latency.so").write_bytes(b"so")
    wrapper = tmp_path / "PENGUIN_BURNER"
    env_file = tmp_path / "env.txt"
    game = tmp_path / "game"
    wrapper.write_text(_wrapper_text("PENGUIN_BURNER"), encoding="utf-8")
    wrapper.chmod(0o755)
    game.write_text(
        "#!/usr/bin/env sh\n"
        f"env | sort > {env_file}\n",
        encoding="utf-8",
    )
    game.chmod(0o755)

    result = subprocess.run(
        [str(wrapper), str(game)],
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "PENGUIN_BURNER_FLATPAK_APP_PATH": str(app_files),
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    captured = env_file.read_text(encoding="utf-8")
    assert f"PENGUIN_BURNER_NATIVE_LAYER_DIR={layer_dir}\n" in captured
    assert f"VK_ADD_IMPLICIT_LAYER_PATH={layer_dir}\n" in captured
    assert "VK_LOADER_LAYERS_ENABLE=VK_LAYER_PENGUINBURNER_latency\n" in captured


def test_flatpak_wrapper_installer_registers_user_vulkan_layer(tmp_path) -> None:
    from common.flatpak_wrappers import install_vulkan_layer_manifest
    from common.flatpak_wrappers import uninstall_vulkan_layer_manifest

    layer_dir = (
        tmp_path
        / "app"
        / "files"
        / "lib"
        / "python3.13"
        / "site-packages"
        / "overlay"
        / "native_layer"
    )
    layer_dir.mkdir(parents=True)
    layer_dir.joinpath("VkLayer_PENGUINBURNER_latency.json").write_text(
        json.dumps(
            {
                "file_format_version": "1.2.1",
                "layer": {
                    "name": "VK_LAYER_PENGUINBURNER_latency",
                    "type": "GLOBAL",
                    "api_version": "1.4.0",
                    "library_path": "./libVkLayer_penguinburner_latency.so",
                    "library_arch": "64",
                    "implementation_version": "1",
                    "description": "PenguinBurner latency telemetry observer",
                    "functions": {
                        "vkGetInstanceProcAddr": "vkGetInstanceProcAddr",
                        "vkGetDeviceProcAddr": "vkGetDeviceProcAddr",
                    },
                    "enable_environment": {
                        "PENGUIN_BURNER_LATENCY_LAYER": "1",
                    },
                    "disable_environment": {
                        "DISABLE_PENGUIN_BURNER_LATENCY_LAYER": "",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    layer_dir.joinpath("libVkLayer_penguinburner_latency.so").write_bytes(b"so")

    target = install_vulkan_layer_manifest(home=tmp_path, layer_dirs=[layer_dir])

    assert target == (
        tmp_path
        / ".local/share/vulkan/implicit_layer.d/VkLayer_PENGUINBURNER_latency.json"
    )
    assert target is not None
    manifest = json.loads(target.read_text(encoding="utf-8"))
    assert manifest["layer"]["library_path"] == str(
        (layer_dir / "libVkLayer_penguinburner_latency.so").resolve()
    )
    assert manifest["layer"]["enable_environment"] == {"PENGUIN_BURNER": "1"}
    assert manifest["layer"]["disable_environment"] == {
        "DISABLE_PENGUIN_BURNER_LATENCY_LAYER": ""
    }

    assert uninstall_vulkan_layer_manifest(home=tmp_path) == target
    assert not target.exists()


def test_flatpak_wrapper_installer_uses_flatpak_app_path_for_layer(tmp_path) -> None:
    from common.flatpak_wrappers import install_vulkan_layer_manifest

    app_files = tmp_path / "flatpak" / "files"
    layer_dir = (
        app_files
        / "lib"
        / "python3.13"
        / "site-packages"
        / "overlay"
        / "native_layer"
    )
    layer_dir.mkdir(parents=True)
    layer_dir.joinpath("VkLayer_PENGUINBURNER_latency.json").write_text(
        json.dumps(
            {
                "file_format_version": "1.2.1",
                "layer": {
                    "name": "VK_LAYER_PENGUINBURNER_latency",
                    "type": "GLOBAL",
                    "api_version": "1.4.0",
                    "library_path": "./libVkLayer_penguinburner_latency.so",
                    "library_arch": "64",
                    "implementation_version": "1",
                    "description": "PenguinBurner latency telemetry observer",
                    "functions": {
                        "vkGetInstanceProcAddr": "vkGetInstanceProcAddr",
                        "vkGetDeviceProcAddr": "vkGetDeviceProcAddr",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    layer_dir.joinpath("libVkLayer_penguinburner_latency.so").write_bytes(b"so")
    flatpak_info = tmp_path / ".flatpak-info"
    flatpak_info.write_text(
        f"[Instance]\napp-path={app_files}\n",
        encoding="utf-8",
    )

    target = install_vulkan_layer_manifest(
        home=tmp_path,
        flatpak_info_path=flatpak_info,
    )

    assert target is not None
    manifest = json.loads(target.read_text(encoding="utf-8"))
    assert manifest["layer"]["library_path"] == str(
        (layer_dir / "libVkLayer_penguinburner_latency.so").resolve()
    )
    assert not manifest["layer"]["library_path"].startswith("/app/")


def test_flatpak_shell_installer_registers_user_vulkan_layer_manifest() -> None:
    script = Path("scripts/install-flatpak-cli-wrappers.sh").read_text(
        encoding="utf-8"
    )

    assert ".local/share/vulkan/implicit_layer.d" in script
    assert '"enable_environment":' in script
    assert '"PENGUIN_BURNER": "1"' in script
    assert "libVkLayer_penguinburner_latency.so" in script
    assert "nvapi-trace.fifo" in script
    assert 'exec 2>&3' in script
    assert "dxvk_nvapi_marker_log_supported" in script
    assert "DXVK_NVAPI_LOG_LEVEL" in script


def test_python_build_requires_native_layer_build_tooling() -> None:
    setup_py = Path("setup.py").read_text(encoding="utf-8")
    manifest = Path("MANIFEST.in").read_text(encoding="utf-8")

    # cmake is a system build dependency (provided by the distro / AUR / RPM
    # specs), not a Python build-system requirement -- see "Fix AUR build: drop
    # python cmake build requirement".
    assert "PENGUIN_BURNER_REQUIRE_NATIVE_LAYER" in setup_py
    assert "root_is_pure = False" in setup_py
    assert 'return "py3", "none", platform_tag' in setup_py
    assert "class BinaryDistribution" in setup_py
    assert "has_ext_modules" in setup_py
    assert "shutil.rmtree(build_root)" in setup_py
    assert "include README.md" in manifest
    assert "overlay/native/latency_layer/CMakeLists.txt" in manifest
    assert "overlay/native/latency_layer/src" in manifest


def test_native_packages_require_native_layer_build_dependencies() -> None:
    arch_pkgbuild = Path("packaging/arch/PKGBUILD").read_text(encoding="utf-8")
    debian_control = Path("packaging/debian/control").read_text(encoding="utf-8")
    debian_rules = Path("packaging/debian/rules").read_text(encoding="utf-8")
    rpm_spec = Path("packaging/rpm/penguin-burner.spec").read_text(encoding="utf-8")
    build_script = Path("scripts/build-python-dist.sh").read_text(encoding="utf-8")
    workflow = Path(".github/workflows/publish-python-package.yml").read_text(
        encoding="utf-8"
    )

    assert "'cmake'" in arch_pkgbuild
    assert "'vulkan-headers'" in arch_pkgbuild
    assert " cmake," in debian_control
    assert " libvulkan-dev," in debian_control
    assert "BuildRequires:  cmake" in rpm_spec
    assert "BuildRequires:  vulkan-headers" in rpm_spec
    for text in (arch_pkgbuild, debian_rules, rpm_spec, build_script, workflow):
        assert "PENGUIN_BURNER_REQUIRE_NATIVE_LAYER" in text


def test_pypi_release_builds_manylinux_native_layer_wheel() -> None:
    build_script = Path("scripts/build-python-dist.sh").read_text(encoding="utf-8")
    workflow = Path(".github/workflows/publish-python-package.yml").read_text(
        encoding="utf-8"
    )

    for text in (build_script, workflow):
        assert "cibuildwheel" in text
        assert "CIBW_ARCHS_LINUX" in text
        assert "x86_64" in text
        assert "CIBW_BUILD" in text
        assert "cp312-manylinux_x86_64" in text
        assert "CIBW_ENVIRONMENT" in text
        assert "CIBW_MANYLINUX_X86_64_IMAGE" in text
        assert "manylinux_2_28" in text
        assert "vulkan-headers" in text


def test_desktop_icons_are_transparent_and_large_enough() -> None:
    for path in (
        Path("packaging/icons/hicolor/256x256/apps/penguin-burner.png"),
        Path("packaging/icons/hicolor/512x512/apps/penguin-burner.png"),
        Path("ui/assets/penguin-burner.png"),
        Path("docs/assets/penguin-burner-logo.png"),
    ):
        width, height, alphas = _png_alpha_channel(path)
        corners = (
            alphas[0],
            alphas[width - 1],
            alphas[(height - 1) * width],
            alphas[-1],
        )
        opaque_indices = [index for index, alpha in enumerate(alphas) if alpha > 0]
        xs = [index % width for index in opaque_indices]
        ys = [index // width for index in opaque_indices]

        assert corners == (0, 0, 0, 0)
        assert max(xs) - min(xs) >= int(width * 0.68)
        assert max(ys) - min(ys) >= int(height * 0.76)


def test_package_installs_shared_subprocess_locale_helper() -> None:
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    py_modules = set(metadata["tool"]["setuptools"]["py-modules"])
    packages = set(metadata["tool"]["setuptools"]["packages"])

    # The repo root is kept minimal: only the CLI entry module ships as a
    # top-level module; everything else lives in a package.
    assert py_modules == {"penguin_burner"}

    # Low-level NVIDIA driver readers -> drivers.nvidia; shared cross-cutting
    # helpers (paths, errors, subprocess locale, CLI output, ascii chart) ->
    # common; Afterburner/LACT import/export helpers -> integrations.
    # The redundant run-from-checkout shims were dropped (the package modules
    # carry their own __main__ / console-script entry points).
    assert {
        "drivers",
        "drivers.nvidia",
        "common",
        "integrations",
        "stability",
        "stability.q2rtx",
        "ui.features.auto_uv",
    } <= packages


def test_fedora_rpm_does_not_hard_require_distro_nvidia_drivers() -> None:
    spec_text = Path("packaging/rpm/penguin-burner.spec").read_text(
        encoding="utf-8"
    )

    assert "Requires:       nvidia-driver" not in spec_text
    assert "Requires:       xorg-x11-drv-nvidia" not in spec_text
    assert "nvidia-driver-cuda >=" not in spec_text
    assert "xorg-x11-drv-nvidia-cuda >=" not in spec_text
    assert "xorg-x11-drv-nvidia-580xx-cuda >=" not in spec_text


def test_native_packages_do_not_hard_require_distro_nvidia_driver_packages() -> None:
    arch_pkgbuild = Path("packaging/arch/PKGBUILD").read_text(encoding="utf-8")
    debian_control = Path("packaging/debian/control").read_text(encoding="utf-8")
    rpm_spec = Path("packaging/rpm/penguin-burner.spec").read_text(encoding="utf-8")

    assert "'nvidia-utils" not in arch_pkgbuild
    assert " nvidia-driver-" not in debian_control
    assert " nvidia-utils-" not in debian_control
    assert "Requires:       nvidia-driver" not in rpm_spec
    assert "Requires:       xorg-x11-drv-nvidia" not in rpm_spec


def test_package_installs_auto_uv_subpackages_and_initial_check() -> None:
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    packages = set(metadata["tool"]["setuptools"]["packages"])
    rpm_spec = Path("packaging/rpm/penguin-burner.spec").read_text(
        encoding="utf-8"
    )

    assert "auto_uv" in packages
    assert "auto_uv.auto_oc" in packages
    assert "cli" in packages
    assert "auto_uv.initial_check" in packages
    assert "auto_uv.efficiency_tune" in packages
    assert "auto_uv.scan_mode" in packages
    assert "auto_uv.final_verification" in packages
    assert "stability" in packages
    assert "stability.q2rtx" in packages
    assert "curve_editors" in packages
    assert "curve_editors.fan" in packages
    assert "curve_editors.uv" in packages
    assert "integrations.afterburner" in packages
    assert "integrations.lact" in packages
    assert "runtime" in packages
    assert "runtime.fan_control" in packages
    assert "runtime.gpu_control" in packages
    assert "runtime.stability_test" in packages
    assert "profiles" in packages
    assert "profiles.verification" in packages
    assert "profiles.uv" in packages
    assert "overlay" in packages
    assert "    stability \\" in rpm_spec


def test_desktop_launcher_is_english_only_nvidia_gpu_tool() -> None:
    desktop_text = Path(
        "packaging/linux/io.github.jpietek.PenguinBurner.desktop"
    ).read_text(encoding="utf-8")

    assert "Name=NVIDIA GPU Automatic Tuning Tool" in desktop_text
    assert "GenericName=NVIDIA GPU Automatic Tuning Tool" in desktop_text
    assert "automatic and adaptive undervolting" in desktop_text
    assert "MSI Afterburner imports" in desktop_text
    assert "LACT exports" in desktop_text
    assert "Exec=penguin-burner" in desktop_text
    assert "Icon=penguin-burner" in desktop_text
    assert "Penguin Burner" in desktop_text
    assert "PenguinBurner" in desktop_text
    assert "StartupWMClass=io.github.jpietek.PenguinBurner" in desktop_text
    assert "Name[" not in desktop_text
    assert "Comment[" not in desktop_text


def test_readme_uses_logo_image_instead_of_emoji_title() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    first_lines = "\n".join(readme.splitlines()[:10])

    assert "docs/assets/penguin-burner-logo.png" in first_lines
    assert "NVIDIA GPU Auto Tuning Linux Tool" in first_lines
    assert "tunes your NVIDIA GPU on Linux" in readme
    assert "automatic and adaptive undervolting" in readme
    assert "MSI Afterburner" in readme
    assert "LACT" in readme
    assert "https://github.com/jpietek/PenguinBurner/issues" in readme
    assert "# 🐧 PenguinBurner 🔥" not in readme


def test_readme_and_flatpak_guide_distinguish_wrappers_from_native_scripts() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    install_doc = Path("docs/install.md").read_text(encoding="utf-8")
    flatpak_doc = Path("docs/flatpak.md").read_text(encoding="utf-8")

    assert "flatpak run io.github.jpietek.PenguinBurner" not in readme
    assert "install-flatpak-cli-wrappers.sh | bash" not in readme
    assert "penguin-burner-install-wrappers" not in readme
    assert "[Flatpak guide](docs/flatpak.md)" in readme
    assert "That single command installs the Flatpak" not in readme
    assert "That single command installs the Flatpak" in flatpak_doc
    assert "Existing Flatpak users should update" in flatpak_doc
    assert (
        "flatpak update --user -y io.github.jpietek.PenguinBurner"
        in flatpak_doc
    )
    assert "To uninstall the Flatpak cleanly" in flatpak_doc
    assert (
        "penguin-burner-install-wrappers io.github.jpietek.PenguinBurner --uninstall"
        in flatpak_doc
    )
    assert (
        "flatpak uninstall --user --delete-data io.github.jpietek.PenguinBurner"
        in flatpak_doc
    )
    assert "flatpak remote-delete --user penguinburner" in flatpak_doc
    assert "`~/.config/PenguinBurner`" in flatpak_doc
    for command in (
        "`penguin-burner`",
        "`pburn`",
        "`penguin-burner-ui`",
        "`pburn-ui`",
        "`penguin-burner-cli`",
        "`pburn-cli`",
        "`PENGUIN_BURNER`",
    ):
        assert command in flatpak_doc
    assert "`PATH`" in flatpak_doc
    assert "refuses to" in flatpak_doc
    assert "existing native/PyPI commands" in flatpak_doc
    assert (
        "PyPI, Flatpak with wrappers, COPR, AUR, and PPA installs run the GUI"
        in readme
    )
    assert (
        "Launch PenguinBurner (`penguin-burner` or `pburn`). Flatpak users without"
        in readme
    )
    assert "## Flatpak" in install_doc
    assert "install-flatpak-cli-wrappers.sh | bash" not in install_doc
    assert "[Flatpak guide](flatpak.md)" in install_doc
    assert "single pasteable command" in flatpak_doc
    assert "penguin-burner-install-wrappers" in install_doc


def _png_alpha_channel(path: Path) -> tuple[int, int, list[int]]:
    data = path.read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    offset = 8
    width = height = bit_depth = color_type = None
    compressed = bytearray()
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        chunk_data = data[offset + 8 : offset + 8 + length]
        offset += length + 12
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(
                ">IIBB",
                chunk_data[:10],
            )
        elif chunk_type == b"IDAT":
            compressed.extend(chunk_data)
        elif chunk_type == b"IEND":
            break

    assert isinstance(width, int)
    assert isinstance(height, int)
    assert bit_depth == 8
    assert color_type == 6

    raw = zlib.decompress(bytes(compressed))
    row_bytes = width * 4
    previous = bytearray(row_bytes)
    pixels = bytearray()
    source_offset = 0
    for _row in range(height):
        filter_type = raw[source_offset]
        source_offset += 1
        row = bytearray(raw[source_offset : source_offset + row_bytes])
        source_offset += row_bytes
        _unfilter_png_row(row, previous, filter_type, bytes_per_pixel=4)
        pixels.extend(row)
        previous = row
    return width, height, list(pixels[3::4])


def _unfilter_png_row(
    row: bytearray,
    previous: bytearray,
    filter_type: int,
    *,
    bytes_per_pixel: int,
) -> None:
    if filter_type == 0:
        return
    for index, value in enumerate(row):
        left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
        up = previous[index]
        upper_left = previous[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
        if filter_type == 1:
            predictor = left
        elif filter_type == 2:
            predictor = up
        elif filter_type == 3:
            predictor = (left + up) // 2
        elif filter_type == 4:
            predictor = _png_paeth(left, up, upper_left)
        else:
            raise AssertionError(f"unsupported PNG filter: {filter_type}")
        row[index] = (value + predictor) & 0xFF


def _png_paeth(left: int, up: int, upper_left: int) -> int:
    estimate = left + up - upper_left
    left_distance = abs(estimate - left)
    up_distance = abs(estimate - up)
    upper_left_distance = abs(estimate - upper_left)
    if left_distance <= up_distance and left_distance <= upper_left_distance:
        return left
    if up_distance <= upper_left_distance:
        return up
    return upper_left
