//! Per-process CPU-pressure sampler from `/proc` — port of
//! `runtime/gpu_control/process_cpu_sampler.py`. Feeds the adaptive CPU-bound
//! guard (via the overlay publisher's `last_cpu_*` metrics).

use std::collections::HashMap;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ProcessCpuUsage {
    pub process_util_pct: Option<i64>,
    pub peak_thread_util_pct: Option<i64>,
    pub peak_thread_id: Option<i64>,
}

struct CpuSample {
    pid: i64,
    process_ticks: i64,
    total_ticks: i64,
    thread_ticks: HashMap<i64, i64>,
}

type ReadText = Box<dyn Fn(&Path) -> Option<String> + Send>;

pub struct ProcessCpuUsageSampler {
    proc_root: PathBuf,
    cpu_count: i64,
    read_text: ReadText,
    last_sample: Option<CpuSample>,
}

fn default_read_text(path: &Path) -> Option<String> {
    std::fs::read(path)
        .ok()
        .map(|b| String::from_utf8_lossy(&b).into_owned())
}

fn cpu_count() -> i64 {
    std::thread::available_parallelism()
        .map(|n| n.get() as i64)
        .unwrap_or(1)
        .max(1)
}

impl Default for ProcessCpuUsageSampler {
    fn default() -> Self {
        Self::new()
    }
}

impl ProcessCpuUsageSampler {
    pub fn new() -> Self {
        ProcessCpuUsageSampler {
            proc_root: PathBuf::from("/proc"),
            cpu_count: cpu_count(),
            read_text: Box::new(default_read_text),
            last_sample: None,
        }
    }

    #[cfg(test)]
    pub fn with_reader(proc_root: &str, cpu_count: i64, read_text: ReadText) -> Self {
        ProcessCpuUsageSampler {
            proc_root: PathBuf::from(proc_root),
            cpu_count: cpu_count.max(1),
            read_text,
            last_sample: None,
        }
    }

    pub fn sample_usage(&mut self, pid: &str) -> ProcessCpuUsage {
        let Some(current) = self.read_sample(pid) else {
            self.last_sample = None;
            return ProcessCpuUsage::default();
        };
        let previous = self.last_sample.take();
        let same_pid = previous.as_ref().is_some_and(|p| p.pid == current.pid);
        let Some(previous) = previous else {
            self.last_sample = Some(current);
            return ProcessCpuUsage::default();
        };
        if !same_pid {
            self.last_sample = Some(current);
            return ProcessCpuUsage::default();
        }

        let process_delta = current.process_ticks - previous.process_ticks;
        let total_delta = current.total_ticks - previous.total_ticks;
        if process_delta < 0 || total_delta <= 0 {
            self.last_sample = Some(current);
            return ProcessCpuUsage::default();
        }

        let mut peak_thread_id: Option<i64> = None;
        let mut peak_thread_delta: i64 = -1;
        for (&tid, &ticks) in &current.thread_ticks {
            if let Some(&prev) = previous.thread_ticks.get(&tid) {
                let delta = ticks - prev;
                if delta >= 0 && delta > peak_thread_delta {
                    peak_thread_id = Some(tid);
                    peak_thread_delta = delta;
                }
            }
        }

        let usage = ProcessCpuUsage {
            process_util_pct: Some(system_cpu_pct(process_delta, total_delta)),
            peak_thread_util_pct: peak_thread_id
                .map(|_| single_cpu_pct(peak_thread_delta, total_delta, self.cpu_count)),
            peak_thread_id,
        };
        self.last_sample = Some(current);
        usage
    }

    fn read(&self, path: &Path) -> Option<String> {
        (self.read_text)(path)
    }

    fn read_sample(&self, pid: &str) -> Option<CpuSample> {
        let pid_value: i64 = pid.trim().parse().ok()?;
        if pid_value <= 0 {
            return None;
        }
        let process_ticks =
            stat_cpu_ticks(&self.read(&self.proc_root.join(pid_value.to_string()).join("stat"))?)?;
        let total_ticks = total_cpu_ticks(&self.read(&self.proc_root.join("stat"))?)?;
        Some(CpuSample {
            pid: pid_value,
            process_ticks,
            total_ticks,
            thread_ticks: self
                .thread_cpu_ticks(&self.proc_root.join(pid_value.to_string()).join("task")),
        })
    }

    fn thread_cpu_ticks(&self, task_dir: &Path) -> HashMap<i64, i64> {
        let mut threads = HashMap::new();
        let Ok(entries) = std::fs::read_dir(task_dir) else {
            return threads;
        };
        for entry in entries.filter_map(Result::ok) {
            let Some(tid) = entry
                .file_name()
                .to_str()
                .and_then(|n| n.parse::<i64>().ok())
            else {
                continue;
            };
            if let Some(text) = self.read(&entry.path().join("stat")) {
                if let Some(ticks) = stat_cpu_ticks(&text) {
                    threads.insert(tid, ticks);
                }
            }
        }
        threads
    }
}

/// `_stat_cpu_ticks`: utime+stime, found after the last `")"`. Returns None on
/// the ValueError cases (missing terminator / too-few fields).
fn stat_cpu_ticks(stat_text: &str) -> Option<i64> {
    let text = stat_text.trim();
    let rparen = text.rfind(')')?;
    let after = &text[rparen + 1..];
    let fields: Vec<&str> = after.split_whitespace().collect();
    if fields.len() <= 12 {
        return None;
    }
    let utime: i64 = fields[11].parse().ok()?;
    let stime: i64 = fields[12].parse().ok()?;
    Some(utime + stime)
}

fn total_cpu_ticks(stat_text: &str) -> Option<i64> {
    for line in stat_text.lines() {
        let fields: Vec<&str> = line.split_whitespace().collect();
        if fields.first() == Some(&"cpu") {
            let mut sum = 0i64;
            for field in &fields[1..] {
                sum += field.parse::<i64>().ok()?;
            }
            return Some(sum);
        }
    }
    None
}

fn system_cpu_pct(delta: i64, total_delta: i64) -> i64 {
    let pct = (delta as f64 / total_delta as f64) * 100.0;
    (super::round_half_even(pct) as i64).clamp(0, 100)
}

fn single_cpu_pct(delta: i64, total_delta: i64, cpu_count: i64) -> i64 {
    let pct = (delta as f64 / total_delta as f64) * cpu_count as f64 * 100.0;
    (super::round_half_even(pct) as i64).clamp(0, 100)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stat_fields_after_comm() {
        // pid (comm) state ppid pgrp ... utime(14th)=fields[11] stime(15th)=fields[12]
        // Build "1 (proc name) S 0 0 0 0 0 0 0 0 0 0 100 50 ..."
        let stat = "1 (proc with spaces) S 0 0 0 0 0 0 0 0 0 0 100 50 0 0";
        assert_eq!(stat_cpu_ticks(stat), Some(150));
    }

    #[test]
    fn total_ticks_sums_aggregate_line() {
        // Fields after "cpu": 100+0+50+800 = 950; the per-core line is ignored.
        let stat = "cpu  100 0 50 800 0 0 0 0 0 0\ncpu0 50 0 25 400\n";
        assert_eq!(total_cpu_ticks(stat), Some(950));
    }

    #[test]
    fn two_samples_same_pid_compute_pct() {
        use std::sync::atomic::{AtomicU32, Ordering};
        use std::sync::Arc;
        // A test-controlled generation flips all /proc reads from a zeroed
        // snapshot (gen 0) to a loaded one (gen 1) between the two samples.
        let generation = Arc::new(AtomicU32::new(0));
        let gen = generation.clone();
        let dir = std::env::temp_dir().join(format!("pb-cpu-{}", std::process::id()));
        let total_stat = dir.join("stat").to_string_lossy().to_string();
        let reader: ReadText = Box::new(move |path: &Path| {
            let loaded = gen.load(Ordering::SeqCst) >= 1;
            let name = path.to_string_lossy().to_string();
            let out = if name == total_stat {
                if loaded {
                    "cpu 100 0 0 900 0 0 0 0 0 0" // total gains 1000
                } else {
                    "cpu 0 0 0 0 0 0 0 0 0 0"
                }
            } else {
                // process or thread stat: utime(11)+stime(12) → 100 when loaded.
                if loaded {
                    "100 (proc name) S 0 0 0 0 0 0 0 0 0 0 60 40"
                } else {
                    "100 (proc name) S 0 0 0 0 0 0 0 0 0 0 0 0"
                }
            };
            Some(out.to_string())
        });
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("100").join("task").join("100")).unwrap();
        let mut sampler = ProcessCpuUsageSampler::with_reader(dir.to_str().unwrap(), 8, reader);
        // First sample → empty (needs two consecutive samples).
        assert_eq!(sampler.sample_usage("100"), ProcessCpuUsage::default());
        generation.store(1, Ordering::SeqCst);
        // Second sample: process 100/1000 → 10%; thread 100/1000*8*100 → 80%.
        let usage = sampler.sample_usage("100");
        assert_eq!(usage.process_util_pct, Some(10));
        assert_eq!(usage.peak_thread_util_pct, Some(80));
        assert_eq!(usage.peak_thread_id, Some(100));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
