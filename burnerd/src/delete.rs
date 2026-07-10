//! `delete_auto_uv_profiles`: delete saved Auto-UV profile files on behalf of
//! the desktop user (the GUI cannot unlink them when a failed chown-back left
//! them root-owned).
//!
//! SECURITY: this daemon runs as root and the paths come from the client, so
//! the prefix check is a security boundary. Every path must canonicalize (all
//! symlinks resolved) to a `*.json` regular file that is a DIRECT child of the
//! effective user's `~/.config/PenguinBurner/auto-uv-profiles` — stricter than
//! the Python allowlist (which permitted nested paths the store never writes).
//! The profiles-dir suffix itself must be symlink-free (the home prefix may be
//! a symlink, e.g. ostree's `/home` → `/var/home`). The unlink goes through a
//! directory fd opened by a component-wise `O_NOFOLLOW` walk in
//! `open_dir_nofollow` (then `unlinkat`), so a parent directory swapped for a
//! symlink AFTER validation makes the walk fail rather than redirect the root
//! deletion elsewhere. Any rejected path fails the whole request naming it;
//! nothing is deleted on a validation failure.

use std::ffi::CString;
use std::fs;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::io::AsRawFd;
use std::path::{Path, PathBuf};

use serde_json::{json, Value};

use crate::paths;

/// Handle the `delete_auto_uv_profiles` request: validate `paths`, delete, and
/// return `{"deleted": [...]}` (canonical paths, in request order, deduped).
pub fn delete_auto_uv_profiles(paths_value: Option<&Value>) -> Result<Value, String> {
    let requested = parse_paths(paths_value)?;
    if requested.is_empty() {
        return Ok(json!({ "deleted": [] }));
    }
    let home = fs::canonicalize(paths::effective_home())
        .map_err(|err| format!("cannot resolve the effective home directory: {err}"))?;
    let profiles_dir = paths::auto_uv_profiles_dir_in(&home);
    let deleted = delete_profile_files(&profiles_dir, &requested)?;
    Ok(json!({ "deleted": deleted }))
}

fn parse_paths(value: Option<&Value>) -> Result<Vec<String>, String> {
    const ERR: &str = "profile paths must be a JSON string list";
    let items = value
        .and_then(Value::as_array)
        .ok_or_else(|| ERR.to_string())?;
    let mut paths = Vec::with_capacity(items.len());
    for item in items {
        match item.as_str() {
            Some(text) => paths.push(text.to_string()),
            None => return Err(ERR.to_string()),
        }
    }
    Ok(paths)
}

/// Validate every requested path against `profiles_dir` (which must already
/// have a canonical home prefix), then unlink the survivors via a pinned dir
/// fd. Returns the canonical deleted paths.
fn delete_profile_files(profiles_dir: &Path, requested: &[String]) -> Result<Vec<String>, String> {
    let canonical_dir = fs::canonicalize(profiles_dir)
        .map_err(|err| format!("cannot resolve the Auto-UV profiles directory: {err}"))?;
    if canonical_dir != *profiles_dir {
        // A symlinked `.config`/`PenguinBurner`/`auto-uv-profiles` could be
        // swapped to point anywhere between validation and unlink; refuse.
        return Err(format!(
            "Auto-UV profiles directory must not resolve through a symlink: {}",
            profiles_dir.display()
        ));
    }

    // Validate everything before deleting anything.
    let mut targets: Vec<PathBuf> = Vec::new();
    for raw in requested {
        let path = Path::new(raw);
        if !path.is_absolute() {
            return Err(format!("profile path must be absolute: {raw}"));
        }
        let canonical = fs::canonicalize(path)
            .map_err(|err| format!("cannot resolve profile path {raw}: {err}"))?;
        if canonical.parent() != Some(canonical_dir.as_path()) {
            return Err(format!(
                "refusing to delete a path outside the Auto-UV profiles directory: {raw}"
            ));
        }
        let is_json = canonical
            .extension()
            .and_then(|ext| ext.to_str())
            .is_some_and(|ext| ext.eq_ignore_ascii_case("json"));
        if !is_json {
            return Err(format!("refusing to delete a non-profile file: {raw}"));
        }
        // `canonicalize` resolved symlinks, so lstat here sees the real inode.
        let metadata = fs::symlink_metadata(&canonical)
            .map_err(|err| format!("cannot resolve profile path {raw}: {err}"))?;
        if !metadata.file_type().is_file() {
            return Err(format!("profile path is not a regular file: {raw}"));
        }
        if !targets.contains(&canonical) {
            targets.push(canonical);
        }
    }

    // Pin the directory via a symlink-free walk, then unlink by basename
    // relative to the pinned fd, so neither an intermediate nor the final
    // component can be redirected by a symlink swapped in after validation.
    let dir = paths::open_dir_nofollow(&canonical_dir)
        .map_err(|err| format!("cannot open the Auto-UV profiles directory: {err}"))?;

    let mut deleted = Vec::with_capacity(targets.len());
    for canonical in targets {
        let name = canonical
            .file_name()
            .and_then(|name| CString::new(name.as_bytes()).ok())
            .ok_or_else(|| {
                format!(
                    "failed to delete {}: invalid file name",
                    canonical.display()
                )
            })?;
        // SAFETY: unlinkat on an owned directory fd with a NUL-terminated
        // relative name; flag 0 does not follow a final symlink. Errors below.
        let rc = unsafe { libc::unlinkat(dir.as_raw_fd(), name.as_ptr(), 0) };
        if rc != 0 {
            let err = std::io::Error::last_os_error();
            // Raced with another deleter (the GUI/CLI path, or a concurrent
            // request): already gone counts as done, parity with the Python
            // store's `except FileNotFoundError: continue`.
            if err.kind() == std::io::ErrorKind::NotFound {
                continue;
            }
            return Err(format!("failed to delete {}: {err}", canonical.display()));
        }
        deleted.push(canonical.display().to_string());
    }
    Ok(deleted)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;
    use std::sync::atomic::{AtomicU32, Ordering};

    static COUNTER: AtomicU32 = AtomicU32::new(0);

    /// A real on-disk fixture: `<tmp>/home/.config/PenguinBurner/auto-uv-profiles`
    /// with two profiles inside and a victim file outside.
    struct Fixture {
        root: PathBuf,
        profiles_dir: PathBuf,
        victim: PathBuf,
    }

    impl Fixture {
        fn new() -> Fixture {
            let n = COUNTER.fetch_add(1, Ordering::SeqCst);
            let root = std::env::temp_dir().join(format!("pb-del-{}-{n}", std::process::id()));
            let _ = fs::remove_dir_all(&root);
            let profiles_dir = root.join(".config/PenguinBurner/auto-uv-profiles");
            fs::create_dir_all(&profiles_dir).unwrap();
            fs::write(profiles_dir.join("auto-uv-profile-a.json"), "{}").unwrap();
            fs::write(profiles_dir.join("auto-uv-profile-b.json"), "{}").unwrap();
            let victim = root.join("victim.json");
            fs::write(&victim, "precious").unwrap();
            Fixture {
                root,
                profiles_dir,
                victim,
            }
        }

        /// The fixture dir canonicalized (temp_dir may sit behind a symlink).
        fn canonical_profiles_dir(&self) -> PathBuf {
            fs::canonicalize(&self.profiles_dir).unwrap()
        }

        fn profile(&self, name: &str) -> String {
            self.profiles_dir.join(name).display().to_string()
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.root);
        }
    }

    fn delete(fixture: &Fixture, paths: &[String]) -> Result<Vec<String>, String> {
        delete_profile_files(&fixture.canonical_profiles_dir(), paths)
    }

    #[test]
    fn deletes_profiles_and_reports_canonical_paths() {
        let fixture = Fixture::new();
        let deleted = delete(
            &fixture,
            &[
                fixture.profile("auto-uv-profile-a.json"),
                fixture.profile("auto-uv-profile-b.json"),
                fixture.profile("auto-uv-profile-a.json"), // duplicate → deduped
            ],
        )
        .unwrap();
        assert_eq!(deleted.len(), 2);
        assert!(deleted[0].ends_with("auto-uv-profile-a.json"));
        assert!(deleted[1].ends_with("auto-uv-profile-b.json"));
        assert!(!fixture.profiles_dir.join("auto-uv-profile-a.json").exists());
        assert!(!fixture.profiles_dir.join("auto-uv-profile-b.json").exists());
    }

    #[test]
    fn empty_request_deletes_nothing() {
        let fixture = Fixture::new();
        assert_eq!(delete(&fixture, &[]).unwrap(), Vec::<String>::new());
    }

    #[test]
    fn rejects_relative_paths() {
        let fixture = Fixture::new();
        let err = delete(&fixture, &["auto-uv-profile-a.json".to_string()]).unwrap_err();
        assert_eq!(err, "profile path must be absolute: auto-uv-profile-a.json");
    }

    #[test]
    fn rejects_dotdot_traversal() {
        let fixture = Fixture::new();
        let attack = fixture.profile("../../../victim.json");
        let err = delete(&fixture, std::slice::from_ref(&attack)).unwrap_err();
        assert_eq!(
            err,
            format!("refusing to delete a path outside the Auto-UV profiles directory: {attack}")
        );
        assert!(fixture.victim.exists());
    }

    #[test]
    fn rejects_absolute_path_outside_the_dir() {
        let fixture = Fixture::new();
        let attack = fixture.victim.display().to_string();
        let err = delete(&fixture, &[attack]).unwrap_err();
        assert!(
            err.starts_with("refusing to delete a path outside"),
            "{err}"
        );
        assert!(fixture.victim.exists());
    }

    #[test]
    fn rejects_symlink_escaping_the_dir() {
        let fixture = Fixture::new();
        let link = fixture.profiles_dir.join("evil.json");
        symlink(&fixture.victim, &link).unwrap();
        let err = delete(&fixture, &[link.display().to_string()]).unwrap_err();
        assert!(
            err.starts_with("refusing to delete a path outside"),
            "{err}"
        );
        // Neither the symlink target nor the symlink itself was deleted.
        assert!(fixture.victim.exists());
        assert!(fs::symlink_metadata(&link).is_ok());
    }

    #[test]
    fn rejects_a_symlinked_profiles_dir() {
        let fixture = Fixture::new();
        // A whole profiles dir that is a symlink (e.g. into /etc) is refused.
        let evil_dir_parent = fixture.root.join("evil-home/.config/PenguinBurner");
        fs::create_dir_all(&evil_dir_parent).unwrap();
        let evil_dir = evil_dir_parent.join("auto-uv-profiles");
        symlink(fixture.root.parent().unwrap(), &evil_dir).unwrap();
        let err = delete_profile_files(&evil_dir, &[fixture.profile("auto-uv-profile-a.json")])
            .unwrap_err();
        assert!(
            err.starts_with("Auto-UV profiles directory must not resolve through a symlink"),
            "{err}"
        );
    }

    #[test]
    fn rejects_non_json_and_nested_and_missing() {
        let fixture = Fixture::new();
        fs::write(fixture.profiles_dir.join("notes.txt"), "x").unwrap();
        let err = delete(&fixture, &[fixture.profile("notes.txt")]).unwrap_err();
        assert!(
            err.starts_with("refusing to delete a non-profile file"),
            "{err}"
        );

        // A nested subdir path is outside the (direct-children-only) allowlist.
        let nested_dir = fixture.profiles_dir.join("nested");
        fs::create_dir_all(&nested_dir).unwrap();
        fs::write(nested_dir.join("deep.json"), "{}").unwrap();
        let err = delete(&fixture, &[fixture.profile("nested/deep.json")]).unwrap_err();
        assert!(
            err.starts_with("refusing to delete a path outside"),
            "{err}"
        );

        // Nonexistent → canonicalization failure names the path.
        let missing = fixture.profile("gone.json");
        let err = delete(&fixture, std::slice::from_ref(&missing)).unwrap_err();
        assert!(
            err.starts_with(&format!("cannot resolve profile path {missing}")),
            "{err}"
        );
    }

    #[test]
    fn rejects_a_directory_named_like_a_profile() {
        let fixture = Fixture::new();
        let dir = fixture.profiles_dir.join("dir.json");
        fs::create_dir_all(&dir).unwrap();
        let err = delete(&fixture, &[dir.display().to_string()]).unwrap_err();
        assert!(
            err.starts_with("profile path is not a regular file"),
            "{err}"
        );
        assert!(dir.exists());
    }

    #[test]
    fn validation_failure_deletes_nothing_at_all() {
        let fixture = Fixture::new();
        let err = delete(
            &fixture,
            &[
                fixture.profile("auto-uv-profile-a.json"), // valid
                fixture.victim.display().to_string(),      // invalid
            ],
        )
        .unwrap_err();
        assert!(err.starts_with("refusing to delete"), "{err}");
        // The valid path was NOT deleted either: validate-all-first.
        assert!(fixture.profiles_dir.join("auto-uv-profile-a.json").exists());
    }

    #[test]
    fn open_dir_nofollow_refuses_an_intermediate_symlink() {
        let fixture = Fixture::new();
        let real = fixture.canonical_profiles_dir();
        // The real symlink-free path opens fine.
        assert!(paths::open_dir_nofollow(&real).is_ok());

        // Point an alternate `.config` at the real one, so `<root>/alt-config`
        // is a symlink an intermediate component of the walk would traverse.
        let alt = fs::canonicalize(&fixture.root).unwrap().join("alt-config");
        symlink(real.parent().unwrap().parent().unwrap(), &alt).unwrap();
        let through_symlink = alt.join("PenguinBurner").join("auto-uv-profiles");
        // The path RESOLVES (canonicalize would follow it), but the O_NOFOLLOW
        // walk refuses the symlinked component — the TOCTOU guard.
        assert!(fs::canonicalize(&through_symlink).is_ok());
        assert!(paths::open_dir_nofollow(&through_symlink).is_err());
    }

    #[test]
    fn parse_paths_shapes() {
        const ERR: &str = "profile paths must be a JSON string list";
        assert_eq!(parse_paths(None).unwrap_err(), ERR);
        assert_eq!(parse_paths(Some(&json!("x"))).unwrap_err(), ERR);
        assert_eq!(parse_paths(Some(&json!([1]))).unwrap_err(), ERR);
        assert_eq!(parse_paths(Some(&json!([null]))).unwrap_err(), ERR);
        assert_eq!(parse_paths(Some(&json!([]))).unwrap(), Vec::<String>::new());
        assert_eq!(parse_paths(Some(&json!(["/a"]))).unwrap(), vec!["/a"]);
    }
}
