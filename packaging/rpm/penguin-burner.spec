Name:           penguin-burner
Version:        0.2
Release:        2%{?dist}
Summary:        NVIDIA GPU automatic undervolting and fine tuning tool

%global debug_package %{nil}

License:        GPL-3.0-or-later
URL:            https://github.com/jpietek/PenguinBurner
Source0:        %{name}-%{version}.tar.gz

ExclusiveArch:  x86_64
BuildRequires:  python3-devel
BuildRequires:  pyproject-rpm-macros
BuildRequires:  python3-pip
BuildRequires:  python3-setuptools
BuildRequires:  python3-wheel
BuildRequires:  desktop-file-utils

Requires:       python3-pyside6
Requires:       python3-colorama
Requires:       python3-pyqtgraph
Requires:       hicolor-icon-theme
Requires:       bash
Requires:       systemd
Requires:       (nvidia-driver-cuda >= 3:580 or xorg-x11-drv-nvidia-cuda >= 3:580 or xorg-x11-drv-nvidia-580xx-cuda >= 3:580)

%description
PenguinBurner is an NVIDIA GPU automatic undervolting tool. It helps you
visualize and manage your GPU fine tuning setup with single voltage/frequency
bin precision to maximize FPS per Watt, potentially leading into +33% and higher
improvements for recent cards. MSI Afterburner imports and LACT exports are
also supported.

It provides a Qt desktop UI, command-line tools, automatic voltage/frequency
scanning, Q2RTX/CUDA stability testing, and optional runtime profile service
installation.

This package is intended for Fedora systems using the proprietary NVIDIA driver,
version 580 or newer, from either Fedora's NVIDIA driver repository or RPM
Fusion.

%prep
%autosetup

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files \
    afterburner \
    auto_oc \
    auto_uv \
    cli \
    initial_check \
    lact \
    manual_fan_curve_editor \
    manual_uv_curve_editor \
    runtime_fan_control \
    runtime_gpu_control \
    runtime_stability_test \
    saved_profile_verification \
    saved_uv_profiles \
    stability \
    ui \
    ascii_chart \
    cli_output \
    cuda_bruteforce_stability \
    dry_run_preview \
    hidden_nvapi_gpu_selection \
    hidden_nvapi_voltage \
    hidden_nvapi_vf \
    import_afterburner_fan_curve \
    import_afterburner_vf_curve \
    nvidia_runtime_defaults \
    nvml_gpu_policy \
    nvml_perf_cap_reason \
    penguin_burner \
    penguin_burner_errors \
    penguin_burner_paths \
    q2rtx_stability \
    runtime_debug \
    runtime_service \
    subprocess_locale

desktop-file-validate %{buildroot}%{_datadir}/applications/io.github.jpietek.PenguinBurner.desktop

%files -f %{pyproject_files}
%license LICENSE
%doc README.md readme-cli.md docs/packaging.md
%{_bindir}/penguin-burner
%{_bindir}/pburn
%{_bindir}/penguin-burner-ui
%{_bindir}/pburn-ui
%{_bindir}/penguin-burner-cli
%{_bindir}/pburn-cli
%{_bindir}/q2rtx-stability
%{_bindir}/cuda-bruteforce-stability
%{_bindir}/penguin-burner-import-afterburner-vf
%{_bindir}/penguin-burner-import-afterburner-fan
%{_datadir}/applications/io.github.jpietek.PenguinBurner.desktop
%{_datadir}/icons/hicolor/256x256/apps/penguin-burner.png
%{_datadir}/icons/hicolor/512x512/apps/penguin-burner.png
%{_datadir}/penguin-burner/PenguinBurner.service
%{_datadir}/penguin-burner/penguin_burner.sh

%changelog
* Wed May 27 2026 PenguinBurner contributors <noreply@github.com> - 0.2-2
- Include nvml_perf_cap_reason in the RPM Python file manifest.

* Tue May 26 2026 PenguinBurner contributors <noreply@github.com> - 0.2-1
- Revise Auto-UV profiles for the 0.2 major release.
- Tune the Efficiency profile for the lowest stable voltage while retaining as much clock as possible.
- Keep Performance profiles focused on revised voltage and clock targets.
- Reapply memory OC offset consistently with runtime V/F curve reapplies, thanks to cbro33's pull request.

* Mon May 25 2026 PenguinBurner contributors <noreply@github.com> - 0.1.8-1
- Replace old Auto-UV internals with the cleaned Auto-UV package namespace.
- Add a separate Performance Auto-OC ladder after the balanced undervolt pass.
- Improve Auto-UV table progress, status, and Auto-OC reporting.
- Keep Performance final selection sorted by measured FPS.

* Wed May 13 2026 PenguinBurner contributors <noreply@github.com> - 0.1.7-1
- Honor time-based Q2RTX verification durations instead of precomputed loop counts.
- Improve PRIME laptop selected-GPU execution and diagnostics.
- Add safer interrupted-probe crash caching.
- Use a conservative Auto-UV default for RTX 3080.

* Fri May 08 2026 PenguinBurner contributors <noreply@github.com> - 0.1.6-1
- Add python3-colorama runtime dependency for pyqtgraph.
- Add borrowed GPU voltage/frequency guardrails for Auto-UV.
- Cap performance-mode Auto-OC by the GPU Performance table voltage.
- Improve final candidate sorting and stopped-scan final candidate selection.

* Mon May 04 2026 PenguinBurner contributors <noreply@github.com> - 0.1.5-1
- Initial COPR package for Fedora 42, 43, and 44.
