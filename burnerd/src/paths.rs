//! Effective desktop-user home resolution + the Auto-UV stop-request marker file.
//!
//! This is a faithful port of `common/penguin_burner_paths.py::_effective_home`
//! and the daemon's stop-request writer/clearer. Every persistence path is rooted
//! at the effective home even when the daemon runs as root, so it must match the
//! Python resolution order exactly or files land in the wrong place.

use std::env;
use std::ffi::{CStr, CString, OsStr};
use std::fs;
use std::io::{self, Write};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::io::{AsRawFd, FromRawFd};
use std::path::{Component, Path, PathBuf};

/// The peercred-gate uid the systemd unit sets — also the fallback drop target
/// for the scan/verification child (`scan.rs`), so the name lives once.
pub(crate) const DAEMON_ALLOWED_UID_ENV: &str = "PENGUIN_BURNER_DAEMON_ALLOWED_UID";

pub(crate) fn nonempty_env(key: &str) -> Option<String> {
    let value = env::var(key).ok()?;
    let trimmed = value.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
}

/// A resolved passwd entry: the user's name and home dir plus its numeric
/// uid/gid. The single lookup surfaces all four so a caller that needs several
/// (chown-back, the child privilege drop) does not pay a second `getpw*_r`.
pub(crate) struct PwEntry {
    pub name: String,
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
        // glibc always fills pw_dir/pw_name, but a broken third-party NSS module
        // may return NULL — dereferencing that would be UB in a root daemon.
        if pwd.pw_dir.is_null() || pwd.pw_name.is_null() {
            return None;
        }
        // SAFETY: pw_dir/pw_name are non-NULL (checked above) and point at
        // NUL-terminated C strings backed by `buf`, which is still alive.
        let dir = unsafe { CStr::from_ptr(pwd.pw_dir) };
        // SAFETY: same.
        let name = unsafe { CStr::from_ptr(pwd.pw_name) };
        return Some(PwEntry {
            name: String::from_utf8_lossy(name.to_bytes()).into_owned(),
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

/// Best-effort marker write. A root daemon opens the existing user-owned config
/// directory and marker with `O_NOFOLLOW`; a non-root dev daemon uses the normal
/// path writer.
fn write_marker(path: &Path, content: &str) {
    if geteuid_is_root() {
        let Some(name) = path.file_name() else {
            return;
        };
        let Ok(Some((config_dir, uid, gid))) = root_config_dir() else {
            return;
        };
        let _ = write_marker_at(&config_dir, name, content, uid, gid);
        return;
    }
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
    if geteuid_is_root() {
        let Some(name) = path.file_name() else {
            return Err(io::Error::from(io::ErrorKind::InvalidInput));
        };
        let Some((config_dir, _, _)) = root_config_dir().map_err(io::Error::other)? else {
            return Ok(());
        };
        let name = CString::new(name.as_bytes())
            .map_err(|_| io::Error::from(io::ErrorKind::InvalidInput))?;
        // SAFETY: unlinkat removes the fixed name relative to a pinned,
        // no-follow config directory. It removes a final symlink itself rather
        // than following it.
        let rc = unsafe { libc::unlinkat(config_dir.as_raw_fd(), name.as_ptr(), 0) };
        if rc == 0 {
            return Ok(());
        }
        let err = io::Error::last_os_error();
        return if err.kind() == io::ErrorKind::NotFound {
            Ok(())
        } else {
            Err(err)
        };
    }
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
    // Parse straight to u32: this rejects negatives, non-numerics, AND
    // out-of-range values like "4294967297" in one step — no `as u32`
    // truncation that would chown files to the wrong user.
    text.trim().parse::<u32>().ok().filter(|&v| v > 0)
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

/// Open an absolute, canonical directory without following a symlink in any
/// component. Walking from `/` with `openat` also pins every parent while the
/// next component is opened, so a user cannot swap a checked directory for a
/// symlink between validation and use.
pub(crate) fn open_dir_nofollow(dir: &Path) -> io::Result<fs::File> {
    let flags = libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_RDONLY;
    let root = CString::new("/").expect("no interior NUL");
    // SAFETY: opening "/" read-only as a directory; `/` is never a symlink.
    let fd = unsafe { libc::open(root.as_ptr(), flags) };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: we exclusively own the freshly-opened fd.
    let mut current = unsafe { fs::File::from_raw_fd(fd) };
    for component in dir.components() {
        let name = match component {
            Component::RootDir => continue,
            Component::Normal(name) => name,
            _ => return Err(io::Error::from(io::ErrorKind::InvalidInput)),
        };
        let name = CString::new(name.as_bytes())
            .map_err(|_| io::Error::from(io::ErrorKind::InvalidInput))?;
        // SAFETY: openat on an owned directory fd with a NUL-terminated name.
        let fd = unsafe { libc::openat(current.as_raw_fd(), name.as_ptr(), flags) };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: we own the new fd; assignment closes the previous one.
        current = unsafe { fs::File::from_raw_fd(fd) };
    }
    Ok(current)
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
/// `include_parents`, the chain up to the effective home) to the desktop user
/// resolved from the env chown-back ladder.
pub(crate) fn claim_desktop_user_ownership(path: &Path, include_parents: bool) {
    // Fast-path the non-root (dev/test) daemon before the env/NSS identity
    // ladder: this sits on the per-tick telemetry/overlay writers, and there is
    // nothing to chown when we are not root. `claim_ownership_for` re-checks the
    // same gate, keeping direct callers fail-safe too.
    if !geteuid_is_root() {
        return;
    }
    let Some((uid, gid)) = effective_desktop_user_ids() else {
        return;
    };
    claim_ownership_for(path, uid, gid, include_parents);
}

/// Chown a root-created path under the desktop user's home to explicit ids
/// (best-effort, root-only).
pub(crate) fn claim_ownership_for(path: &Path, uid: u32, gid: u32, include_parents: bool) {
    if !geteuid_is_root() {
        return;
    }
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

const OPEN_DIR_FLAGS: libc::c_int =
    libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_RDONLY;

fn open_optional_dir_at(parent: &fs::File, name: &OsStr) -> Result<Option<fs::File>, String> {
    let name = CString::new(name.as_bytes())
        .map_err(|_| "directory name contains NUL".to_string())?;
    // SAFETY: openat on an owned directory fd with a NUL-terminated relative name.
    let fd = unsafe { libc::openat(parent.as_raw_fd(), name.as_ptr(), OPEN_DIR_FLAGS) };
    if fd >= 0 {
        // SAFETY: we exclusively own the freshly-opened fd.
        return Ok(Some(unsafe { fs::File::from_raw_fd(fd) }));
    }
    let err = io::Error::last_os_error();
    if err.kind() == io::ErrorKind::NotFound {
        Ok(None)
    } else {
        Err(err.to_string())
    }
}

fn directory_owner_uid(dir: &fs::File) -> io::Result<u32> {
    // SAFETY: an all-zero stat is a valid output buffer for fstat.
    let mut stat: libc::stat = unsafe { std::mem::zeroed() };
    // SAFETY: `dir` is an open fd and `stat` points to writable storage.
    if unsafe { libc::fstat(dir.as_raw_fd(), &mut stat) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(stat.st_uid)
}

fn require_directory_owner(dir: &fs::File, uid: u32, label: &str) -> Result<(), String> {
    let owner =
        directory_owner_uid(dir).map_err(|err| format!("cannot inspect {label}: {err}"))?;
    if owner == uid {
        Ok(())
    } else {
        Err(format!(
            "refusing unsafe {label}: expected uid {uid}, found uid {owner}"
        ))
    }
}

/// Validate the real `~/.config/PenguinBurner` path before a de-rooted child is
/// launched. The home comes from passwd, and each existing directory is opened
/// relative to a pinned parent with `O_NOFOLLOW`. Missing directories are fine:
/// the unprivileged child creates them. Symlinked or root/foreign-owned config
/// directories are rejected instead of repaired by the root daemon.
fn open_config_dir_for(home: &Path, uid: u32) -> Result<Option<fs::File>, String> {
    let canonical_home = fs::canonicalize(home)
        .map_err(|err| format!("cannot resolve desktop home {}: {err}", home.display()))?;
    let home_dir = open_dir_nofollow(&canonical_home)
        .map_err(|err| format!("cannot safely open desktop home {}: {err}", home.display()))?;
    require_directory_owner(&home_dir, uid, "desktop home")?;

    let Some(dot_config) = open_optional_dir_at(&home_dir, OsStr::new(".config"))
        .map_err(|err| format!("refusing unsafe .config directory: {err}"))?
    else {
        return Ok(None);
    };
    require_directory_owner(&dot_config, uid, ".config directory")?;

    let Some(config_dir) = open_optional_dir_at(&dot_config, OsStr::new("PenguinBurner"))
        .map_err(|err| format!("refusing unsafe PenguinBurner config directory: {err}"))?
    else {
        return Ok(None);
    };
    require_directory_owner(&config_dir, uid, "PenguinBurner config directory")?;
    Ok(Some(config_dir))
}

pub(crate) fn validate_config_tree_for(home: &Path, uid: u32) -> Result<(), String> {
    open_config_dir_for(home, uid).map(|_| ())
}

fn root_config_dir() -> Result<Option<(fs::File, u32, u32)>, String> {
    let (uid, gid) = effective_desktop_user_ids()
        .ok_or_else(|| "desktop user identity is unavailable".to_string())?;
    let config_dir = open_config_dir_for(&effective_home(), uid)?;
    Ok(config_dir.map(|dir| (dir, uid, gid)))
}

fn write_marker_at(
    config_dir: &fs::File,
    name: &OsStr,
    content: &str,
    uid: u32,
    gid: u32,
) -> Result<(), String> {
    let name = CString::new(name.as_bytes())
        .map_err(|_| "marker name contains NUL".to_string())?;
    let temp = CString::new(format!(".{}.tmp", name.to_string_lossy()))
        .map_err(|_| "temporary marker name contains NUL".to_string())?;
    // A fixed temporary name is safe here: unlinkat/renameat never follow its
    // final symlink, and O_EXCL turns a replacement race into a harmless error.
    // SAFETY: all names are relative to the pinned config directory.
    unsafe {
        libc::unlinkat(config_dir.as_raw_fd(), temp.as_ptr(), 0);
    }
    let fd = unsafe {
        libc::openat(
            config_dir.as_raw_fd(),
            temp.as_ptr(),
            libc::O_WRONLY
                | libc::O_CREAT
                | libc::O_EXCL
                | libc::O_NOFOLLOW
                | libc::O_CLOEXEC,
            0o600,
        )
    };
    if fd < 0 {
        return Err(format!(
            "cannot safely create stop marker: {}",
            io::Error::last_os_error()
        ));
    }
    // SAFETY: we exclusively own the freshly-opened fd.
    let mut marker = unsafe { fs::File::from_raw_fd(fd) };
    // SAFETY: fchown targets the newly-created, pinned marker inode.
    if unsafe { libc::fchown(marker.as_raw_fd(), uid, gid) } != 0 {
        return Err(format!(
            "cannot prepare stop marker: {}",
            io::Error::last_os_error()
        ));
    }
    marker
        .write_all(content.as_bytes())
        .map_err(|err| format!("cannot write stop marker: {err}"))?;
    drop(marker);
    // SAFETY: renameat atomically replaces the marker basename itself; an
    // attacker-supplied destination symlink is not followed.
    if unsafe {
        libc::renameat(
            config_dir.as_raw_fd(),
            temp.as_ptr(),
            config_dir.as_raw_fd(),
            name.as_ptr(),
        )
    } != 0
    {
        return Err(format!(
            "cannot install stop marker: {}",
            io::Error::last_os_error()
        ));
    }
    Ok(())
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
    use std::os::unix::fs::symlink;
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
    fn parse_positive_int_rejects_out_of_range() {
        assert_eq!(parse_positive_int("1000"), Some(1000));
        assert_eq!(parse_positive_int(" 42 "), Some(42));
        assert_eq!(parse_positive_int("0"), None);
        assert_eq!(parse_positive_int("-5"), None);
        // Would previously wrap to 1 via `as u32` and chown to the wrong user.
        assert_eq!(parse_positive_int("4294967297"), None);
        assert_eq!(parse_positive_int("99999999999999999999"), None);
        assert_eq!(parse_positive_int("abc"), None);
    }

    #[test]
    fn config_operations_do_not_follow_symlinks() {
        let root = std::env::temp_dir().join(format!("pb-handoff-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let home = root.join("home");
        let dot_config = home.join(".config");
        let victim = root.join("victim");
        fs::create_dir_all(&dot_config).unwrap();
        fs::create_dir(dot_config.join("PenguinBurner")).unwrap();
        fs::create_dir_all(victim.join("must-stay-owned")).unwrap();
        // SAFETY: getuid is always safe.
        let uid = unsafe { libc::getuid() };
        assert!(validate_config_tree_for(&home, uid).is_ok());

        fs::remove_dir(dot_config.join("PenguinBurner")).unwrap();
        symlink(&victim, dot_config.join("PenguinBurner")).unwrap();

        let error = validate_config_tree_for(&home, uid).unwrap_err();
        assert!(
            error.contains("unsafe PenguinBurner config directory"),
            "unexpected error: {error}"
        );
        assert!(victim.join("must-stay-owned").is_dir());

        fs::remove_file(dot_config.join("PenguinBurner")).unwrap();
        let config_dir_path = dot_config.join("PenguinBurner");
        fs::create_dir(&config_dir_path).unwrap();
        let config_dir = open_config_dir_for(&home, uid).unwrap().unwrap();
        // SAFETY: getgid is always safe.
        let gid = unsafe { libc::getgid() };
        write_marker_at(
            &config_dir,
            OsStr::new("auto-uv-stop-requested"),
            "safe marker\n",
            uid,
            gid,
        )
        .unwrap();
        assert_eq!(
            fs::read_to_string(config_dir_path.join("auto-uv-stop-requested")).unwrap(),
            "safe marker\n"
        );
        fs::remove_file(config_dir_path.join("auto-uv-stop-requested")).unwrap();

        let victim_file = root.join("must-not-be-overwritten");
        fs::write(&victim_file, "precious").unwrap();
        symlink(
            &victim_file,
            config_dir_path.join("auto-uv-stop-requested"),
        )
        .unwrap();
        write_marker_at(
            &config_dir,
            OsStr::new("auto-uv-stop-requested"),
            "stop\n",
            uid,
            gid,
        )
        .unwrap();
        assert_eq!(fs::read_to_string(&victim_file).unwrap(), "precious");
        assert_eq!(
            fs::read_to_string(config_dir_path.join("auto-uv-stop-requested")).unwrap(),
            "stop\n"
        );

        let _ = fs::remove_dir_all(&root);
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
