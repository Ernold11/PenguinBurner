//! Wake-free RTD3 (runtime D3 / D3cold) detection.
//!
//! Every read here is a cached kernel string: PCI sysfs power attributes and
//! the NVIDIA procfs `power` file report state without initializing or waking
//! the GPU. That is the point — the daemon decides whether deep sleep needs
//! protecting BEFORE it ever touches NVML, because an NVML init (or any
//! `/dev/nvidia*` open) would itself wake the GPU and hold it in D0.

use std::fs;
use std::path::{Path, PathBuf};

/// `Runtime D3 status:` as reported by `/proc/driver/nvidia/gpus/<addr>/power`.
///
/// `Unknown` covers a missing file, a missing line, and the literal `?` the
/// driver prints while the GPU has never been initialized.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RuntimeD3Config {
    FineGrained,
    CoarseGrained,
    Disabled,
    NotSupported,
    Unknown,
}

impl RuntimeD3Config {
    pub fn enabled(self) -> bool {
        matches!(self, Self::FineGrained | Self::CoarseGrained)
    }
}

pub fn parse_runtime_d3_config(power_file_text: &str) -> RuntimeD3Config {
    let Some(value) = power_file_text
        .lines()
        .find_map(|line| line.strip_prefix("Runtime D3 status:").map(str::trim))
    else {
        return RuntimeD3Config::Unknown;
    };
    let lower = value.to_ascii_lowercase();
    if lower.starts_with("enabled") {
        if lower.contains("coarse-grained") {
            RuntimeD3Config::CoarseGrained
        } else {
            // "Enabled (fine-grained)"; a bare "Enabled" is treated as
            // fine-grained because the distinction only affects reporting.
            RuntimeD3Config::FineGrained
        }
    } else if lower.starts_with("disabled") {
        RuntimeD3Config::Disabled
    } else if lower.starts_with("not supported") {
        RuntimeD3Config::NotSupported
    } else {
        RuntimeD3Config::Unknown
    }
}

/// Kernel runtime-PM state from `<device>/power/runtime_status`
/// (Documentation/ABI/testing/sysfs-devices-power).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RuntimePmStatus {
    Active,
    Suspending,
    Suspended,
    Resuming,
    Error,
    Unsupported,
    Unknown,
}

impl RuntimePmStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Active => "active",
            Self::Suspending => "suspending",
            Self::Suspended => "suspended",
            Self::Resuming => "resuming",
            Self::Error => "error",
            Self::Unsupported => "unsupported",
            Self::Unknown => "unknown",
        }
    }
}

pub fn parse_runtime_pm_status(text: &str) -> RuntimePmStatus {
    match text.trim() {
        "active" => RuntimePmStatus::Active,
        "suspending" => RuntimePmStatus::Suspending,
        "suspended" => RuntimePmStatus::Suspended,
        "resuming" => RuntimePmStatus::Resuming,
        "error" => RuntimePmStatus::Error,
        "unsupported" => RuntimePmStatus::Unsupported,
        _ => RuntimePmStatus::Unknown,
    }
}

/// Normalize a PCI address to the sysfs form `0000:01:00.0`.
///
/// NVML reports an eight-hex-digit domain (`00000000:01:00.0`); sysfs and the
/// NVIDIA procfs tree use four. Both are accepted, as is a domain-less
/// `01:00.0`.
pub fn sysfs_pci_addr(raw: &str) -> Option<String> {
    let lower = raw.trim().to_ascii_lowercase();
    let parts: Vec<&str> = lower.split(':').collect();
    let (domain, bus, devfn) = match parts.as_slice() {
        [domain, bus, devfn] => (u32::from_str_radix(domain, 16).ok()?, *bus, *devfn),
        [bus, devfn] => (0, *bus, *devfn),
        _ => return None,
    };
    let (dev, func) = devfn.split_once('.')?;
    Some(format!(
        "{domain:04x}:{:02x}:{:02x}.{:x}",
        u8::from_str_radix(bus, 16).ok()?,
        u8::from_str_radix(dev, 16).ok()?,
        u8::from_str_radix(func, 16).ok()?,
    ))
}

/// Read-only view of the PCI sysfs tree and the NVIDIA procfs tree, with
/// injectable roots so tests can point it at a fake filesystem.
pub struct Rtd3Probe {
    pci_devices_root: PathBuf,
    nvidia_gpus_root: PathBuf,
}

const SYSFS_ROOT_ENV: &str = "PENGUIN_BURNERD_TEST_RTD3_SYSFS";
const PROC_ROOT_ENV: &str = "PENGUIN_BURNERD_TEST_RTD3_PROC";

impl Rtd3Probe {
    /// The live system trees. Test seam (never set in production): the
    /// `PENGUIN_BURNERD_TEST_RTD3_*` env roots swap in a fake tree so the
    /// integration harness can simulate an RTD3 laptop on any machine.
    pub fn system() -> Self {
        let env_path = |name: &str, default: &str| {
            std::env::var_os(name)
                .filter(|v| !v.is_empty())
                .map_or_else(|| PathBuf::from(default), PathBuf::from)
        };
        Self {
            pci_devices_root: env_path(SYSFS_ROOT_ENV, "/sys/bus/pci/devices"),
            nvidia_gpus_root: env_path(PROC_ROOT_ENV, "/proc/driver/nvidia/gpus"),
        }
    }

    #[cfg(test)]
    pub fn with_roots(pci_devices_root: &Path, nvidia_gpus_root: &Path) -> Self {
        Self {
            pci_devices_root: pci_devices_root.to_path_buf(),
            nvidia_gpus_root: nvidia_gpus_root.to_path_buf(),
        }
    }

    fn read_trimmed(path: &Path) -> Option<String> {
        fs::read_to_string(path)
            .ok()
            .map(|text| text.trim().to_string())
    }

    pub fn device_present(&self, addr: &str) -> bool {
        self.pci_devices_root.join(addr).is_dir()
    }

    /// True when the device at `addr` is an NVIDIA display device (vendor
    /// 0x10de, class 0x03xxxx). Guards the persisted-spec hint: an address is
    /// only trusted if the hardware behind it is still the expected GPU.
    pub fn is_nvidia_gpu(&self, addr: &str) -> bool {
        let device = self.pci_devices_root.join(addr);
        let vendor = Self::read_trimmed(&device.join("vendor"));
        let class = Self::read_trimmed(&device.join("class"));
        vendor.as_deref() == Some("0x10de") && class.is_some_and(|class| class.starts_with("0x03"))
    }

    /// First NVIDIA display-class device in the PCI tree, in stable name
    /// order. Hybrid laptops have exactly one.
    pub fn first_nvidia_gpu_addr(&self) -> Option<String> {
        let entries = fs::read_dir(&self.pci_devices_root).ok()?;
        let mut addrs: Vec<String> = entries
            .filter_map(|entry| entry.ok())
            .filter_map(|entry| entry.file_name().into_string().ok())
            .collect();
        addrs.sort();
        addrs.into_iter().find(|addr| self.is_nvidia_gpu(addr))
    }

    pub fn runtime_d3_config(&self, addr: &str) -> RuntimeD3Config {
        let path = self.nvidia_gpus_root.join(addr).join("power");
        match fs::read_to_string(path) {
            Ok(text) => parse_runtime_d3_config(&text),
            Err(_) => RuntimeD3Config::Unknown,
        }
    }

    /// `power/control`: `auto` means the kernel may runtime-suspend the
    /// device; `on` pins it awake (the state without the NVIDIA udev rules).
    pub fn power_control(&self, addr: &str) -> Option<String> {
        Self::read_trimmed(&self.pci_devices_root.join(addr).join("power/control"))
    }

    pub fn runtime_pm_status(&self, addr: &str) -> RuntimePmStatus {
        match Self::read_trimmed(&self.pci_devices_root.join(addr).join("power/runtime_status")) {
            Some(text) => parse_runtime_pm_status(&text),
            None => RuntimePmStatus::Unknown,
        }
    }

    /// Cumulative runtime-PM residency: `power/runtime_active_time` and
    /// `power/runtime_suspended_time`, milliseconds since boot
    /// (Documentation/ABI/testing/sysfs-devices-power). Cached kernel
    /// counters — reading them never wakes the device. `None` per counter
    /// when the file is missing or unparsable.
    pub fn runtime_pm_times(&self, addr: &str) -> (Option<u64>, Option<u64>) {
        let read = |file: &str| {
            Self::read_trimmed(&self.pci_devices_root.join(addr).join("power").join(file))
                .and_then(|text| text.parse().ok())
        };
        (
            read("runtime_active_time"),
            read("runtime_suspended_time"),
        )
    }
}

/// The deep-sleep mode for one GPU. `Unknown` is a deliberate third state:
/// "not decidable yet" (device not bound, driver power file still `?`) must
/// keep the daemon detached rather than fall through to the desktop
/// NVML-at-boot path, otherwise the boot race reintroduces the pinned-GPU bug.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DeepSleepMode {
    Mobile { fine_grained: bool },
    Desktop { reason: &'static str },
    Unknown { reason: &'static str },
}

pub fn assess(probe: &Rtd3Probe, addr: &str) -> DeepSleepMode {
    if !probe.device_present(addr) {
        return DeepSleepMode::Unknown {
            reason: "PCI device not visible yet",
        };
    }
    match probe.runtime_d3_config(addr) {
        config if config.enabled() => match probe.power_control(addr).as_deref() {
            Some("auto") => DeepSleepMode::Mobile {
                fine_grained: config == RuntimeD3Config::FineGrained,
            },
            Some(_) => DeepSleepMode::Desktop {
                reason: "power/control is not 'auto' (runtime PM disabled by policy)",
            },
            None => DeepSleepMode::Unknown {
                reason: "power/control not readable",
            },
        },
        RuntimeD3Config::Disabled | RuntimeD3Config::NotSupported => DeepSleepMode::Desktop {
            reason: "driver reports runtime D3 disabled or unsupported",
        },
        _ => DeepSleepMode::Unknown {
            reason: "driver power file missing or GPU not initialized",
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn fake_tree(
        d3_line: Option<&str>,
        control: Option<&str>,
        runtime_status: Option<&str>,
    ) -> (tempfile::TempDir, Rtd3Probe, &'static str) {
        let addr = "0000:01:00.0";
        let root = tempfile::tempdir().expect("tempdir");
        let device = root.path().join("sys").join(addr);
        let power = device.join("power");
        fs::create_dir_all(&power).unwrap();
        fs::write(device.join("vendor"), "0x10de\n").unwrap();
        fs::write(device.join("class"), "0x030000\n").unwrap();
        if let Some(control) = control {
            fs::write(power.join("control"), format!("{control}\n")).unwrap();
        }
        if let Some(status) = runtime_status {
            fs::write(power.join("runtime_status"), format!("{status}\n")).unwrap();
        }
        let proc_gpu = root.path().join("proc").join(addr);
        fs::create_dir_all(&proc_gpu).unwrap();
        if let Some(line) = d3_line {
            fs::write(
                proc_gpu.join("power"),
                format!("Runtime D3 status:          {line}\nVideo Memory:               Off\n"),
            )
            .unwrap();
        }
        let probe = Rtd3Probe::with_roots(&root.path().join("sys"), &root.path().join("proc"));
        (root, probe, addr)
    }

    #[test]
    fn runtime_pm_times_read_the_kernel_counters_or_none() {
        let (root, probe, addr) = fake_tree(None, None, Some("suspended"));
        // Missing files (old kernels): both counters absent.
        assert_eq!(probe.runtime_pm_times(addr), (None, None));

        let power = root.path().join("sys").join(addr).join("power");
        fs::write(power.join("runtime_active_time"), "5123\n").unwrap();
        fs::write(power.join("runtime_suspended_time"), "60789\n").unwrap();
        assert_eq!(probe.runtime_pm_times(addr), (Some(5123), Some(60789)));

        // Garbage stays None per counter, not a panic.
        fs::write(power.join("runtime_active_time"), "not-a-number\n").unwrap();
        assert_eq!(probe.runtime_pm_times(addr), (None, Some(60789)));
    }

    #[test]
    fn parses_runtime_d3_config_variants() {
        assert_eq!(
            parse_runtime_d3_config("Runtime D3 status:          Enabled (fine-grained)\n"),
            RuntimeD3Config::FineGrained
        );
        assert_eq!(
            parse_runtime_d3_config("Runtime D3 status:          Enabled (coarse-grained)\n"),
            RuntimeD3Config::CoarseGrained
        );
        assert_eq!(
            parse_runtime_d3_config("Runtime D3 status:          Disabled by default\n"),
            RuntimeD3Config::Disabled
        );
        assert_eq!(
            parse_runtime_d3_config("Runtime D3 status:          Not supported\n"),
            RuntimeD3Config::NotSupported
        );
        assert_eq!(
            parse_runtime_d3_config("Runtime D3 status:          ?\n"),
            RuntimeD3Config::Unknown
        );
        assert_eq!(parse_runtime_d3_config(""), RuntimeD3Config::Unknown);
    }

    #[test]
    fn normalizes_pci_addresses() {
        assert_eq!(
            sysfs_pci_addr("00000000:01:00.0").as_deref(),
            Some("0000:01:00.0")
        );
        assert_eq!(
            sysfs_pci_addr("0000:01:00.0").as_deref(),
            Some("0000:01:00.0")
        );
        assert_eq!(sysfs_pci_addr("01:00.0").as_deref(), Some("0000:01:00.0"));
        assert_eq!(
            sysfs_pci_addr("00000000:C1:00.0").as_deref(),
            Some("0000:c1:00.0")
        );
        assert_eq!(sysfs_pci_addr(""), None);
        assert_eq!(sysfs_pci_addr("not-a-pci-addr"), None);
    }

    #[test]
    fn selects_mobile_on_fine_grained_rtd3_with_auto_control() {
        let (_root, probe, addr) =
            fake_tree(Some("Enabled (fine-grained)"), Some("auto"), Some("suspended"));
        assert_eq!(
            assess(&probe, addr),
            DeepSleepMode::Mobile { fine_grained: true }
        );
        assert_eq!(probe.runtime_pm_status(addr), RuntimePmStatus::Suspended);
        assert_eq!(probe.first_nvidia_gpu_addr().as_deref(), Some(addr));
    }

    #[test]
    fn selects_desktop_when_control_is_pinned_on() {
        let (_root, probe, addr) =
            fake_tree(Some("Enabled (fine-grained)"), Some("on"), Some("active"));
        assert!(matches!(
            assess(&probe, addr),
            DeepSleepMode::Desktop { .. }
        ));
    }

    #[test]
    fn selects_desktop_when_rtd3_is_disabled() {
        let (_root, probe, addr) =
            fake_tree(Some("Disabled by default"), Some("auto"), Some("active"));
        assert!(matches!(
            assess(&probe, addr),
            DeepSleepMode::Desktop { .. }
        ));
    }

    #[test]
    fn unknown_when_driver_power_file_missing_or_uninitialized() {
        let (_root, probe, addr) = fake_tree(None, Some("auto"), Some("active"));
        assert!(matches!(
            assess(&probe, addr),
            DeepSleepMode::Unknown { .. }
        ));
        let (_root, probe, addr) = fake_tree(Some("?"), Some("auto"), Some("active"));
        assert!(matches!(
            assess(&probe, addr),
            DeepSleepMode::Unknown { .. }
        ));
    }

    #[test]
    fn unknown_when_device_absent() {
        let (_root, probe, _) = fake_tree(Some("Enabled (fine-grained)"), Some("auto"), None);
        assert!(matches!(
            assess(&probe, "0000:99:00.0"),
            DeepSleepMode::Unknown { .. }
        ));
    }
}
