//! Embed a git build id so a running daemon is identifiable at commit
//! granularity. `version` alone (0.7.8 across a whole PR branch) let a stale
//! /usr/libexec binary impersonate a fresh build through three debugging
//! rounds of issue #30; the id in the startup journal line and the `status`
//! response makes "which build is actually running" a one-command check.

use std::process::Command;

fn main() {
    // Re-run when the checkout's head moves; both paths are absent in sdist
    // builds, where cargo ignores the stale-path directives.
    println!("cargo:rerun-if-changed=../.git/HEAD");
    println!("cargo:rerun-if-changed=../.git/refs");
    let build_id = Command::new("git")
        .args(["rev-parse", "--short", "HEAD"])
        .output()
        .ok()
        .filter(|output| output.status.success())
        .and_then(|output| String::from_utf8(output.stdout).ok())
        .map(|sha| sha.trim().to_string())
        .filter(|sha| !sha.is_empty())
        // Non-git trees (sdist/tarball builds) still build; they just cannot
        // claim a commit.
        .unwrap_or_else(|| "unknown".to_string());
    println!("cargo:rustc-env=PENGUIN_BURNERD_BUILD_ID={build_id}");
}
