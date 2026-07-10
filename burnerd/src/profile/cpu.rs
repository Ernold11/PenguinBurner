//! Per-process CPU-pressure sampler for the adaptive CPU-bound guard.

use std::collections::HashMap;

use procfs::process::Process;
use procfs::{CurrentSI, KernelStats};

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ProcessCpuUsage {
    pub process_util_pct: Option<i64>,
    pub peak_thread_util_pct: Option<i64>,
    pub peak_thread_id: Option<i64>,
}

#[derive(Clone)]
struct CpuSample {
    pid: i64,
    process_ticks: u64,
    total_ticks: u64,
    thread_ticks: HashMap<i64, u64>,
}

type SampleSource = Box<dyn Fn(&str) -> Option<CpuSample> + Send>;

pub struct ProcessCpuUsageSampler {
    cpu_count: i64,
    source: SampleSource,
    last_sample: Option<CpuSample>,
}

impl Default for ProcessCpuUsageSampler {
    fn default() -> Self {
        Self::new()
    }
}

impl ProcessCpuUsageSampler {
    pub fn new() -> Self {
        Self {
            cpu_count: cpu_count(),
            source: Box::new(procfs_sample),
            last_sample: None,
        }
    }

    #[cfg(test)]
    fn with_source(cpu_count: i64, source: SampleSource) -> Self {
        Self {
            cpu_count: cpu_count.max(1),
            source,
            last_sample: None,
        }
    }

    pub fn sample_usage(&mut self, pid: &str) -> ProcessCpuUsage {
        let Some(current) = (self.source)(pid) else {
            self.last_sample = None;
            return ProcessCpuUsage::default();
        };
        let Some(previous) = self.last_sample.replace(current.clone()) else {
            return ProcessCpuUsage::default();
        };
        if previous.pid != current.pid {
            return ProcessCpuUsage::default();
        }

        let Some(process_delta) = current.process_ticks.checked_sub(previous.process_ticks) else {
            return ProcessCpuUsage::default();
        };
        let Some(total_delta) = current.total_ticks.checked_sub(previous.total_ticks) else {
            return ProcessCpuUsage::default();
        };
        if total_delta == 0 {
            return ProcessCpuUsage::default();
        }

        let peak = current
            .thread_ticks
            .iter()
            .filter_map(|(&tid, &ticks)| {
                ticks
                    .checked_sub(*previous.thread_ticks.get(&tid)?)
                    .map(|delta| (tid, delta))
            })
            .max_by_key(|(_, delta)| *delta);

        ProcessCpuUsage {
            process_util_pct: Some(system_cpu_pct(process_delta, total_delta)),
            peak_thread_util_pct: peak
                .map(|(_, delta)| single_cpu_pct(delta, total_delta, self.cpu_count)),
            peak_thread_id: peak.map(|(tid, _)| tid),
        }
    }
}

fn procfs_sample(pid: &str) -> Option<CpuSample> {
    let pid_value: i32 = pid.trim().parse().ok()?;
    if pid_value <= 0 {
        return None;
    }
    let process = Process::new(pid_value).ok()?;
    let stat = process.stat().ok()?;
    let kernel = KernelStats::current().ok()?;
    let mut thread_ticks = HashMap::new();
    for task in process.tasks().ok()?.flatten() {
        if let Ok(task_stat) = task.stat() {
            thread_ticks.insert(
                i64::from(task.tid),
                task_stat.utime.saturating_add(task_stat.stime),
            );
        }
    }
    Some(CpuSample {
        pid: i64::from(pid_value),
        process_ticks: stat.utime.saturating_add(stat.stime),
        total_ticks: cpu_time_ticks(&kernel.total),
        thread_ticks,
    })
}

fn cpu_time_ticks(cpu: &procfs::CpuTime) -> u64 {
    cpu.user
        .saturating_add(cpu.nice)
        .saturating_add(cpu.system)
        .saturating_add(cpu.idle)
        .saturating_add(cpu.iowait.unwrap_or(0))
        .saturating_add(cpu.irq.unwrap_or(0))
        .saturating_add(cpu.softirq.unwrap_or(0))
        .saturating_add(cpu.steal.unwrap_or(0))
        .saturating_add(cpu.guest.unwrap_or(0))
        .saturating_add(cpu.guest_nice.unwrap_or(0))
}

fn cpu_count() -> i64 {
    std::thread::available_parallelism()
        .map(|count| count.get() as i64)
        .unwrap_or(1)
        .max(1)
}

fn system_cpu_pct(delta: u64, total_delta: u64) -> i64 {
    let pct = (delta as f64 / total_delta as f64) * 100.0;
    (super::round_half_even(pct) as i64).clamp(0, 100)
}

fn single_cpu_pct(delta: u64, total_delta: u64, cpu_count: i64) -> i64 {
    let pct = (delta as f64 / total_delta as f64) * cpu_count as f64 * 100.0;
    (super::round_half_even(pct) as i64).clamp(0, 100)
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicU32, Ordering};
    use std::sync::Arc;

    use super::*;

    fn sample(pid: i64, process: u64, total: u64, thread: u64) -> CpuSample {
        CpuSample {
            pid,
            process_ticks: process,
            total_ticks: total,
            thread_ticks: HashMap::from([(pid, thread)]),
        }
    }

    #[test]
    fn two_samples_same_pid_compute_pct() {
        let generation = Arc::new(AtomicU32::new(0));
        let source_generation = generation.clone();
        let source: SampleSource = Box::new(move |_| {
            if source_generation.load(Ordering::SeqCst) == 0 {
                Some(sample(100, 0, 0, 0))
            } else {
                Some(sample(100, 100, 1000, 100))
            }
        });
        let mut sampler = ProcessCpuUsageSampler::with_source(8, source);

        assert_eq!(sampler.sample_usage("100"), ProcessCpuUsage::default());
        generation.store(1, Ordering::SeqCst);
        let usage = sampler.sample_usage("100");

        assert_eq!(usage.process_util_pct, Some(10));
        assert_eq!(usage.peak_thread_util_pct, Some(80));
        assert_eq!(usage.peak_thread_id, Some(100));
    }

    #[test]
    fn pid_change_resets_the_delta_window() {
        let generation = Arc::new(AtomicU32::new(0));
        let source_generation = generation.clone();
        let source: SampleSource = Box::new(move |_| {
            let pid = 100 + i64::from(source_generation.load(Ordering::SeqCst));
            Some(sample(pid, 100, 1000, 100))
        });
        let mut sampler = ProcessCpuUsageSampler::with_source(8, source);

        assert_eq!(sampler.sample_usage("100"), ProcessCpuUsage::default());
        generation.store(1, Ordering::SeqCst);
        assert_eq!(sampler.sample_usage("101"), ProcessCpuUsage::default());
    }

    #[test]
    fn procfs_source_reads_the_current_process() {
        let pid = std::process::id().to_string();
        let sample = procfs_sample(&pid).expect("read current process from procfs");
        assert_eq!(sample.pid, i64::from(std::process::id()));
        assert!(sample.total_ticks > 0);
        assert!(!sample.thread_ticks.is_empty());
    }
}
