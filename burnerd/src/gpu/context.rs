//! Short-lived GPU-context observation for the RTD3 watcher.
//!
//! Unlike the general GPU RPC backend, one call owns one context-only NVML
//! session and drops it before returning. It never opens hidden NVAPI sessions
//! and never enters the shared RPC registry.

use std::env;
use std::path::PathBuf;

use super::{backend, mock::MockGpu, GpuBackend, GpuContextPids, GpuError};

pub(crate) use backend::ContextProbeError;

const MOCK_ENV: &str = "PENGUIN_BURNERD_TEST_MOCK_GPU";
const MOCK_FAIL_ENV: &str = "PENGUIN_BURNERD_TEST_MOCK_GPU_FAIL";
const MOCK_CONTEXT_PIDS_ENV: &str = "PENGUIN_BURNERD_TEST_MOCK_GPU_CONTEXT_PIDS";
const MOCK_LIFETIME_ENV: &str = "PENGUIN_BURNERD_TEST_MOCK_GPU_LIFETIME_FILE";

/// Observe live graphics/compute contexts without retaining NVIDIA state.
/// The production and test adapters both finish their complete open/query/drop
/// lifetime before this function returns, including query-error paths.
pub(crate) fn context_pids_ephemeral(gpu_index: u32) -> Result<GpuContextPids, ContextProbeError> {
    if env::var_os(MOCK_ENV).is_some_and(|value| !value.is_empty()) {
        return mock_context_pids_ephemeral(gpu_index);
    }
    backend::context_pids_ephemeral(gpu_index)
}

fn mock_context_pids_ephemeral(gpu_index: u32) -> Result<GpuContextPids, ContextProbeError> {
    let mut mock = MockGpu::new();
    mock.gpu_index = gpu_index;
    mock.context_pids_file = env::var_os(MOCK_CONTEXT_PIDS_ENV)
        .filter(|value| !value.is_empty())
        .map(PathBuf::from);
    if let Some(path) = env::var_os(MOCK_LIFETIME_ENV)
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
    {
        mock.track_lifetime(path);
    }
    if env::var(MOCK_FAIL_ENV).as_deref() == Ok("gpu_context_pids") {
        mock.inject_failure(
            "gpu_context_pids",
            GpuError::other("gpu_context_pids mock failure", 0),
        );
    }
    let result = mock.gpu_context_pids();
    drop(mock);
    result.map_err(ContextProbeError::Query)
}
