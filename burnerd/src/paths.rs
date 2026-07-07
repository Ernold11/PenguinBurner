//! Effective desktop-user home resolution + the Auto-UV stop-request marker file.
//!
//! This is a faithful port of `common/penguin_burner_paths.py::_effective_home`
//! and the daemon's stop-request writer/clearer. Every persistence path is rooted
//! at the effective home even when the daemon runs as root, so it must match the
//! Python resolution order exactly or files land in the wrong place.

use std::env;
use std::ffi::{CStr, CString, OsStr};
use std::fs;
use std::io;
use std::os::unix::ffi::OsStrExt;
use std::path::{Path, PathBuf};

pub(crate) fn nonempty_env(key: &str) -> Option<String> {
    let value = env::var(key).ok()?;
    let trimmed = value.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
}

/// A resolved passwd entry: the user's home dir plus its numeric uid/gid. The
/// single lookup surfaces all three so a caller that needs both ids (chown-back)
/// does not pay a second `getpw*_r`.
pub(crate) struct PwEntry {
    pub dir: PathBuf,
    pub uid: u32,
    pub gid: u32,
}

/// Run a reentrant passwd lookup (`getpw*_r`), growing the buffer on `ERANGE`.
/// `call(pwd, buf, len, result)` performs the actual `getpwnam_r`/`getpwuid_r`.
fn pw_lookup(
    mut call: impl FnMut(
        *mut libc::passwd,
        *mut libc::c_char,
        usize,
        *mut *mut libc::passwd,
    ) -> libc::c_int,
) -> Option<PwEntry> {
    let mut buf = vec![0u8; 4096];
    loop {
        // SAFETY: an all-zero passwd is a valid initial value.
        let mut pwd: libc::passwd = unsafe { std::mem::zeroed() };
        let mut result: *mut libc::passwd = std::ptr::null_mut();
        let rc = call(
            &mut pwd,
            buf.as_mut_ptr() as *mut libc::c_char,
            buf.len(),
            &mut result,
        );
        if rc == libc::ERANGE {
            buf.resize(buf.len() * 2, 0);
            continue;
        }
        if rc != 0 || result.is_null() {
            return None;
        }
        // SAFETY: on success pw_dir is a valid NUL-terminated C string.
        let dir = unsafe { CStr::from_ptr(pwd.pw_dir) };
        return Some(PwEntry {
            dir: PathBuf::from(OsStr::from_bytes(dir.to_bytes())),
            uid: pwd.pw_uid,
            gid: pwd.pw_gid,
        });
    }
}

/// Look up a passwd entry by name (`pwd.getpwnam(...)`).
pub(crate) fn pw_by_name(name: &str) -> Option<PwEntry> {
    let c_name = CString::new(name).ok()?;
    pw_lookup(|pwd, buf, len, result| {
        // SAFETY: all pointers/lengths are valid for the call.
        unsafe { libc::getpwnam_r(c_name.as_ptr(), pwd, buf, len, result) }
    })
}

/// Look up a passwd entry by uid (`pwd.getpwuid(...)`).
pub(crate) fn pw_by_uid(uid: u32) -> Option<PwEntry> {
    pw_lookup(|pwd, buf, len, result| {
        // SAFETY: all pointers/lengths are valid for the call.
        unsafe { libc::getpwuid_r(uid as libc::uid_t, pwd, buf, len, result) }
    })
}

/// Equivalent of `Path.home()` == `os.path.expanduser("~")`: `$HOME` if present,
/// else the passwd entry for the real uid.
fn home_fallback() -> PathBuf {
    if let Some(home) = env::var_os("HOME") {
        return PathBuf::from(home);
    }
    // SAFETY: getuid is always safe.
    let uid = unsafe { libc::getuid() };
    pw_by_uid(uid)
        .map(|e| e.dir)
        .unwrap_or_else(|| PathBuf::from("/"))
}

/// Minimal `Path(text).expanduser()`: expands a leading `~`, `~/...`, or
/// `~user[/...]`. An unknown `~user` is left unchanged (parity with Python).
fn expanduser(text: &str) -> PathBuf {
    if !text.starts_with('~') {
        return PathBuf::from(text);
    }
    if text == "~" {
        return home_fallback();
    }
    if let Some(rest) = text.strip_prefix("~/") {
        return home_fallback().join(rest);
    }
    let after = &text[1..];
    let (user, rest) = match after.find('/') {
        Some(idx) => (&after[..idx], Some(&after[idx + 1..])),
        None => (after, None),
    };
    match pw_by_name(user).map(|e| e.dir) {
        Some(dir) => match rest {
            Some(r) => dir.join(r),
            None => dir,
        },
        None => PathBuf::from(text),
    }
}

/// Port of `common/penguin_burner_paths.py::_effective_home`. First hit wins.
pub fn effective_home() -> PathBuf {
    if let Some(home) = nonempty_env("PENGUIN_BURNER_HOME") {
        return expanduser(&home);
    }
    for key in ["SUDO_USER", "PENGUIN_BURNER_Q2RTX_USER"] {
        if let Some(user) = nonempty_env(key) {
            if let Some(entry) = pw_by_name(&user) {
                return entry.dir;
            }
        }
    }
    if let Some(uid_text) = nonempty_env("PENGUIN_BURNER_Q2RTX_UID") {
        if let Ok(uid) = uid_text.parse::<u32>() {
            if let Some(entry) = pw_by_uid(uid) {
                return entry.dir;
            }
        }
    }
    home_fallback()
}

/// `<home>/.config/PenguinBurner` for an explicit home. The single place the
/// `.config/PenguinBurner` spelling lives.
pub fn config_dir_in(home: &Path) -> PathBuf {
    home.join(".config").join("PenguinBurner")
}

/// `<home>/.config/PenguinBurner`.
pub fn user_config_dir() -> PathBuf {
    config_dir_in(&effective_home())
}

/// `<home>/.config/PenguinBurner/auto-uv-profiles` for an explicit home (the
/// deletion flow passes a *canonicalized* home so the suffix can be checked
/// symlink-free). Mirrors `profiles/uv/profile_store.py::auto_uv_profiles_dir`.
pub fn auto_uv_profiles_dir_in(home: &Path) -> PathBuf {
    config_dir_in(home).join("auto-uv-profiles")
}

/// `<config>/auto-uv-stop-requested` (a marker file, no `.json`).
pub fn auto_uv_stop_request_path() -> PathBuf {
    user_config_dir().join("auto-uv-stop-requested")
}

/// `<config>/profile-verify-stop-requested` — the cooperative stop marker the
/// `--stability-test` child polls (same file the Qt `VerifyController` used).
pub fn profile_verify_stop_request_path() -> PathBuf {
    user_config_dir().join("profile-verify-stop-requested")
}

/// Write the profile-verification stop marker (best-effort). The child only
/// checks existence; the content is informational.
pub fn write_profile_verify_stop_request() {
    write_marker(
        &profile_verify_stop_request_path(),
        "stop requested by PenguinBurner daemon client\n",
    );
}

/// Remove the profile-verification stop marker (`NotFound` is success).
pub fn clear_profile_verify_stop_request() -> io::Result<()> {
    clear_marker(&profile_verify_stop_request_path())
}

/// Write the stop-request marker (best-effort, all errors swallowed). Content is
/// byte-identical to the Python daemon's `_write_auto_uv_stop_request`.
pub fn write_auto_uv_stop_request(abort_final_choice: bool) {
    let reason = if abort_final_choice {
        "abort-final-choice"
    } else {
        "offer-final-choice"
    };
    write_marker(
        &auto_uv_stop_request_path(),
        &format!("stop requested by PenguinBurner daemon client\nreason={reason}\n"),
    );
}

/// Best-effort marker write: create the parent dir, then write `content`. All
/// errors are swallowed (parity with the Python daemon's marker writers).
fn write_marker(path: &Path, content: &str) {
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    let _ = fs::write(path, content);
}

/// Remove the stop-request marker. `NotFound` is treated as success (parity with
/// `clear_auto_uv_stop_request`'s ignore-`FileNotFoundError`); any other error is
/// surfaced so the scan launcher can report a stale-clear failure.
pub fn clear_auto_uv_stop_request() -> io::Result<()> {
    clear_marker(&auto_uv_stop_request_path())
}

fn clear_marker(path: &Path) -> io::Result<()> {
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(err) if err.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(err) => Err(err),
    }
}

// --- desktop-user ownership + XDG/cache ladder ------------------------------
// The daemon runs as root but persists overlay/telemetry files under the desktop
// user's home; these helpers resolve that user's ids/paths and chown root-created
// files back to it. Shared by the telemetry writer and the latency receiver.

pub(crate) fn geteuid_is_root() -> bool {
    // SAFETY: geteuid is always safe.
    unsafe { libc::geteuid() == 0 }
}

fn parse_positive_int(text: &str) -> Option<u32> {
    match text.trim().parse::<i64>() {
        Ok(v) if v > 0 => Some(v as u32),
        _ => None,
    }
}

/// `effective_desktop_user_ids` → `(uid, gid)` for chown-back. One passwd lookup
/// resolves both ids when the numeric env hints are absent.
fn effective_desktop_user_ids() -> Option<(u32, u32)> {
    let mut uid = nonempty_env("PENGUIN_BURNER_Q2RTX_UID")
        .or_else(|| nonempty_env("SUDO_UID"))
        .and_then(|t| parse_positive_int(&t));
    let mut gid = nonempty_env("PENGUIN_BURNER_Q2RTX_GID")
        .or_else(|| nonempty_env("SUDO_GID"))
        .and_then(|t| parse_positive_int(&t));
    if uid.is_none() || gid.is_none() {
        if let Some(user) =
            nonempty_env("PENGUIN_BURNER_Q2RTX_USER").or_else(|| nonempty_env("SUDO_USER"))
        {
            if let Some(entry) = pw_by_name(&user) {
                uid = uid.or(Some(entry.uid));
                gid = gid.or(Some(entry.gid));
            }
        }
    }
    match (uid, gid) {
        (Some(u), Some(g)) => Some((u, g)),
        _ => None,
    }
}

fn is_under_desktop_path(path: &Path, uid: u32) -> bool {
    let home = effective_home();
    if path.starts_with(&home) {
        return true;
    }
    let run_user = PathBuf::from(format!("/run/user/{uid}"));
    path.starts_with(&run_user)
}

/// A path as a NUL-terminated C string for a raw libc call (`None` on interior
/// NUL). Shared by the `lchown`/`chmod` best-effort ownership writers.
pub(crate) fn cpath(path: &Path) -> Option<CString> {
    CString::new(path.as_os_str().as_bytes()).ok()
}

fn lchown(path: &Path, uid: u32, gid: u32) {
    if let Some(c) = cpath(path) {
        // SAFETY: lchown on a valid path; errors are ignored (best-effort).
        unsafe {
            libc::lchown(c.as_ptr(), uid, gid);
        }
    }
}

/// `claim_desktop_user_ownership` — chown a root-created path (and, with
/// `include_parents`, the chain up to the effective home) to the desktop user.
pub(crate) fn claim_desktop_user_ownership(path: &Path, include_parents: bool) {
    if !geteuid_is_root() {
        return;
    }
    let Some((uid, gid)) = effective_desktop_user_ids() else {
        return;
    };
    if !is_under_desktop_path(path, uid) {
        return;
    }
    if include_parents {
        let home = effective_home();
        let mut parents: Vec<PathBuf> = Vec::new();
        let mut current = path.parent();
        while let Some(dir) = current {
            if dir == home || !dir.starts_with(&home) {
                break;
            }
            parents.push(dir.to_path_buf());
            current = dir.parent();
        }
        for dir in parents.iter().rev() {
            lchown(dir, uid, gid);
        }
    }
    lchown(path, uid, gid);
}

/// The `/run/user/<uid>/penguin-burner` directory when the process runs as root
/// and a desktop uid resolves (SUDO_UID digits, else SUDO_USER passwd uid) and the
/// runtime dir exists. `is_root` differs by caller — a deliberate divergence: the
/// overlay writer passes the EFFECTIVE-uid check (`geteuid`), the latency receiver
/// the REAL-uid check (`getuid`). The trailing filename is joined by the caller.
pub(crate) fn run_user_penguin_dir(is_root: bool) -> Option<PathBuf> {
    if !is_root {
        return None;
    }
    if let Some(uid) = nonempty_env("SUDO_UID").filter(|s| s.chars().all(|c| c.is_ascii_digit())) {
        let candidate = PathBuf::from("/run/user").join(&uid);
        if candidate.exists() {
            return Some(candidate.join("penguin-burner"));
        }
    }
    if let Some(user) = nonempty_env("SUDO_USER") {
        if let Some(entry) = pw_by_name(&user) {
            let candidate = PathBuf::from("/run/user").join(entry.uid.to_string());
            if candidate.exists() {
                return Some(candidate.join("penguin-burner"));
            }
        }
    }
    None
}

/// The desktop user's `~/.cache/penguin-burner`: `$HOME` (unless empty or
/// `/root`), else the SUDO_UID passwd home, else the SUDO_USER passwd home. The
/// trailing filename (`overlay-state.txt` / `latency.sock`) is joined by callers.
pub(crate) fn desktop_cache_dir() -> Option<PathBuf> {
    let home = std::env::var("HOME").unwrap_or_default();
    let home = home.trim();
    if !home.is_empty() && home != "/root" {
        return Some(PathBuf::from(home).join(".cache").join("penguin-burner"));
    }
    if let Some(uid) = nonempty_env("SUDO_UID").filter(|s| s.chars().all(|c| c.is_ascii_digit())) {
        if let Some(entry) = uid.parse::<u32>().ok().and_then(pw_by_uid) {
            return Some(entry.dir.join(".cache").join("penguin-burner"));
        }
    }
    if let Some(user) = nonempty_env("SUDO_USER") {
        if let Some(entry) = pw_by_name(&user) {
            return Some(entry.dir.join(".cache").join("penguin-burner"));
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    // Env is process-global; serialize the env-mutating tests.
    static ENV_LOCK: Mutex<()> = Mutex::new(());

    struct EnvGuard {
        saved: Vec<(&'static str, Option<String>)>,
    }
    impl EnvGuard {
        fn new(keys: &[&'static str]) -> Self {
            let saved = keys.iter().map(|k| (*k, env::var(k).ok())).collect();
            for k in keys {
                env::remove_var(k);
            }
            EnvGuard { saved }
        }
        fn set(&self, key: &str, value: &str) {
            env::set_var(key, value);
        }
    }
    impl Drop for EnvGuard {
        fn drop(&mut self) {
            for (key, value) in &self.saved {
                match value {
                    Some(v) => env::set_var(key, v),
                    None => env::remove_var(key),
                }
            }
        }
    }

    const KEYS: &[&str] = &[
        "PENGUIN_BURNER_HOME",
        "SUDO_USER",
        "PENGUIN_BURNER_Q2RTX_USER",
        "PENGUIN_BURNER_Q2RTX_UID",
    ];

    #[test]
    fn home_override_wins() {
        let _lock = ENV_LOCK.lock().unwrap();
        let guard = EnvGuard::new(KEYS);
        guard.set("PENGUIN_BURNER_HOME", "/tmp/pb-home-xyz");
        assert_eq!(effective_home(), PathBuf::from("/tmp/pb-home-xyz"));
    }

    #[test]
    fn home_override_expands_tilde() {
        let _lock = ENV_LOCK.lock().unwrap();
        let guard = EnvGuard::new(KEYS);
        // root always resolves via getpwuid even if HOME is unset.
        let expected = super::home_fallback().join("sub");
        guard.set("PENGUIN_BURNER_HOME", "~/sub");
        assert_eq!(effective_home(), expected);
    }

    #[test]
    fn sudo_user_resolves_to_passwd_home() {
        let _lock = ENV_LOCK.lock().unwrap();
        let guard = EnvGuard::new(KEYS);
        guard.set("SUDO_USER", "root");
        // root's home is whatever the passwd db says; just assert it resolved to it.
        assert_eq!(effective_home(), super::pw_by_name("root").unwrap().dir);
    }

    #[test]
    fn q2rtx_uid_resolves_to_passwd_home() {
        let _lock = ENV_LOCK.lock().unwrap();
        let guard = EnvGuard::new(KEYS);
        guard.set("PENGUIN_BURNER_Q2RTX_UID", "0");
        assert_eq!(effective_home(), super::pw_by_uid(0).unwrap().dir);
    }

    #[test]
    fn config_and_stop_paths_are_derived_from_home() {
        let _lock = ENV_LOCK.lock().unwrap();
        let guard = EnvGuard::new(KEYS);
        guard.set("PENGUIN_BURNER_HOME", "/tmp/pb-home-abc");
        assert_eq!(
            user_config_dir(),
            PathBuf::from("/tmp/pb-home-abc/.config/PenguinBurner")
        );
        assert_eq!(
            auto_uv_stop_request_path(),
            PathBuf::from("/tmp/pb-home-abc/.config/PenguinBurner/auto-uv-stop-requested")
        );
    }

    #[test]
    fn stop_request_file_content_is_byte_exact() {
        let _lock = ENV_LOCK.lock().unwrap();
        let guard = EnvGuard::new(KEYS);
        let dir = std::env::temp_dir().join(format!("pb-stop-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        guard.set("PENGUIN_BURNER_HOME", dir.to_str().unwrap());

        write_auto_uv_stop_request(false);
        let text = fs::read_to_string(auto_uv_stop_request_path()).unwrap();
        assert_eq!(
            text,
            "stop requested by PenguinBurner daemon client\nreason=offer-final-choice\n"
        );

        write_auto_uv_stop_request(true);
        let text = fs::read_to_string(auto_uv_stop_request_path()).unwrap();
        assert_eq!(
            text,
            "stop requested by PenguinBurner daemon client\nreason=abort-final-choice\n"
        );

        clear_auto_uv_stop_request().unwrap();
        assert!(!auto_uv_stop_request_path().exists());
        // Clearing a missing marker is not an error.
        clear_auto_uv_stop_request().unwrap();
        let _ = fs::remove_dir_all(&dir);
    }
}
