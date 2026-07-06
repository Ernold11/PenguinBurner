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

fn nonempty_env(key: &str) -> Option<String> {
    let value = env::var(key).ok()?;
    let trimmed = value.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
}

/// Run a reentrant passwd lookup (`getpw*_r`), growing the buffer on `ERANGE`,
/// and return the entry's `pw_dir`. `call(pwd, buf, len, result)` performs the
/// actual `getpwnam_r`/`getpwuid_r`.
fn pw_dir_lookup(
    mut call: impl FnMut(
        *mut libc::passwd,
        *mut libc::c_char,
        usize,
        *mut *mut libc::passwd,
    ) -> libc::c_int,
) -> Option<PathBuf> {
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
        return Some(PathBuf::from(OsStr::from_bytes(dir.to_bytes())));
    }
}

/// Look up a user's home directory by name (`pwd.getpwnam(...).pw_dir`).
fn pw_dir_by_name(name: &str) -> Option<PathBuf> {
    let c_name = CString::new(name).ok()?;
    pw_dir_lookup(|pwd, buf, len, result| {
        // SAFETY: all pointers/lengths are valid for the call.
        unsafe { libc::getpwnam_r(c_name.as_ptr(), pwd, buf, len, result) }
    })
}

/// Look up a home directory by uid (`pwd.getpwuid(...).pw_dir`).
fn pw_dir_by_uid(uid: u32) -> Option<PathBuf> {
    pw_dir_lookup(|pwd, buf, len, result| {
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
    pw_dir_by_uid(uid).unwrap_or_else(|| PathBuf::from("/"))
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
    match pw_dir_by_name(user) {
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
            if let Some(dir) = pw_dir_by_name(&user) {
                return dir;
            }
        }
    }
    if let Some(uid_text) = nonempty_env("PENGUIN_BURNER_Q2RTX_UID") {
        if let Ok(uid) = uid_text.parse::<u32>() {
            if let Some(dir) = pw_dir_by_uid(uid) {
                return dir;
            }
        }
    }
    home_fallback()
}

/// `<home>/.config/PenguinBurner`.
pub fn user_config_dir() -> PathBuf {
    effective_home().join(".config").join("PenguinBurner")
}

/// `<config>/auto-uv-stop-requested` (a marker file, no `.json`).
pub fn auto_uv_stop_request_path() -> PathBuf {
    user_config_dir().join("auto-uv-stop-requested")
}

/// Write the stop-request marker (best-effort, all errors swallowed). Content is
/// byte-identical to the Python daemon's `_write_auto_uv_stop_request`.
pub fn write_auto_uv_stop_request(abort_final_choice: bool) {
    let path = auto_uv_stop_request_path();
    if let Some(parent) = path.parent() {
        let _ = fs::create_dir_all(parent);
    }
    let reason = if abort_final_choice {
        "abort-final-choice"
    } else {
        "offer-final-choice"
    };
    let content = format!("stop requested by PenguinBurner daemon client\nreason={reason}\n");
    let _ = fs::write(&path, content);
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
        assert_eq!(effective_home(), super::pw_dir_by_name("root").unwrap());
    }

    #[test]
    fn q2rtx_uid_resolves_to_passwd_home() {
        let _lock = ENV_LOCK.lock().unwrap();
        let guard = EnvGuard::new(KEYS);
        guard.set("PENGUIN_BURNER_Q2RTX_UID", "0");
        assert_eq!(effective_home(), super::pw_dir_by_uid(0).unwrap());
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
