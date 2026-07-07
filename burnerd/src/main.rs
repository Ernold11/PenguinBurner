mod api;
mod argvspec;
mod delete;
mod gpu;
mod gpu_rpc;
mod logging;
mod paths;
mod profile;
mod scan;
mod server;
mod supervisor;

use std::env;
use std::fs;
use std::os::linux::net::SocketAddrExt;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::net::{SocketAddr, UnixDatagram};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use supervisor::Supervisor;

const DEFAULT_SOCKET: &str = "/run/penguin-burnerd.sock";
const WATCHDOG_INTERVAL: Duration = Duration::from_secs(10);

fn main() {
    std::process::exit(run());
}

fn run() -> i32 {
    // Block SIGINT/SIGTERM before ANY thread is spawned. Threads inherit the
    // signal mask at spawn, and the sigwait design only works when every other
    // thread blocks these signals: a thread spawned earlier (the autostart
    // engine and everything it spawns) would receive a process-directed SIGTERM
    // directly and kill the process with NO cleanup — no fan-auto restore, no
    // clock-lock release, no socket unlink.
    block_termination_signals();

    let socket_path = match parse_args() {
        Ok(path) => path,
        Err(message) => {
            eprintln!("{message}");
            return 2;
        }
    };

    logging::info(&format!(
        "penguin-burnerd {} starting",
        env!("CARGO_PKG_VERSION")
    ));

    let sup = Arc::new(Mutex::new(Supervisor::new()));

    // Start the persisted runtime profile before binding the socket (parity with
    // `serve_daemon_api`, which runs autostart first).
    supervisor::start_autostart_if_configured(&sup);

    // Route SIGINT/SIGTERM to a dedicated thread that performs a clean shutdown.
    {
        let sup = sup.clone();
        let socket_path = socket_path.clone();
        thread::Builder::new()
            .name("penguin-burner-signals".to_string())
            .spawn(move || wait_and_shutdown(&sup, &socket_path))
            .expect("spawn signal thread");
    }

    let listener = match server::bind(&socket_path) {
        Ok(listener) => listener,
        Err(err) => {
            logging::error(&format!("{err}"));
            return 1;
        }
    };
    logging::info(&format!("listening on {}", socket_path.display()));

    sd_notify("READY=1");
    {
        let sup = sup.clone();
        thread::Builder::new()
            .name("penguin-burner-watchdog".to_string())
            .spawn(move || watchdog_loop(&sup))
            .expect("spawn watchdog thread");
    }

    // Blocks forever; the process exits via the signal thread.
    server::serve(listener, sup);
    0
}

fn parse_args() -> Result<PathBuf, String> {
    let mut socket = PathBuf::from(DEFAULT_SOCKET);
    let mut args = env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "serve" => {}
            "--socket" => {
                socket = PathBuf::from(args.next().ok_or("--socket requires a value")?);
            }
            other if other.starts_with("--socket=") => {
                socket = PathBuf::from(&other["--socket=".len()..]);
            }
            "-h" | "--help" => {
                return Err("usage: penguin-burnerd [serve] [--socket PATH]".to_string());
            }
            other => return Err(format!("unknown argument: {other}")),
        }
    }
    Ok(socket)
}

// --- signal handling ---------------------------------------------------------

fn termination_sigset() -> libc::sigset_t {
    // SAFETY: sigemptyset/sigaddset operate on the owned, zeroed set.
    unsafe {
        let mut set: libc::sigset_t = std::mem::zeroed();
        libc::sigemptyset(&mut set);
        libc::sigaddset(&mut set, libc::SIGINT);
        libc::sigaddset(&mut set, libc::SIGTERM);
        set
    }
}

/// Block SIGINT/SIGTERM in every thread so only the dedicated `sigwait` thread
/// receives them (clean cleanup runs in normal thread context, not a handler).
fn block_termination_signals() {
    let set = termination_sigset();
    // SAFETY: standard process-wide signal-mask update.
    unsafe {
        libc::pthread_sigmask(libc::SIG_BLOCK, &set, std::ptr::null_mut());
    }
}

fn wait_and_shutdown(sup: &Arc<Mutex<Supervisor>>, socket_path: &Path) -> ! {
    let set = termination_sigset();
    let mut received: libc::c_int = 0;
    // SAFETY: `set` and `received` are valid for the call.
    unsafe {
        libc::sigwait(&set, &mut received);
    }
    logging::info(&format!("received signal {received}, shutting down"));
    supervisor::shutdown(sup);
    let _ = fs::remove_file(socket_path);
    std::process::exit(0);
}

// --- systemd sd_notify + watchdog -------------------------------------------

fn watchdog_loop(sup: &Arc<Mutex<Supervisor>>) {
    if env::var_os("NOTIFY_SOCKET").is_none() {
        return;
    }
    loop {
        thread::sleep(WATCHDOG_INTERVAL);
        // Health = the supervisor mutex is acquirable (not wedged mid-operation).
        let healthy = !matches!(sup.try_lock(), Err(std::sync::TryLockError::WouldBlock));
        if healthy {
            sd_notify("WATCHDOG=1");
        }
    }
}

/// `sd_notify` datagram via std (no libsystemd). Best-effort — every failure is
/// a silent no-op; supports both filesystem and abstract (`@` or NUL prefix)
/// `$NOTIFY_SOCKET` addresses.
fn sd_notify(message: &str) {
    let address = match env::var_os("NOTIFY_SOCKET") {
        Some(value) if !value.is_empty() => value,
        _ => return,
    };
    let raw = address.as_bytes();
    let Ok(socket) = UnixDatagram::unbound() else {
        return;
    };
    if matches!(raw.first(), Some(&b'@') | Some(&0)) {
        // Abstract namespace: strip the marker byte; the kernel address is a
        // leading NUL plus the name, which `from_abstract_name` builds for us.
        let Ok(addr) = SocketAddr::from_abstract_name(&raw[1..]) else {
            return;
        };
        let _ = socket.send_to_addr(message.as_bytes(), &addr);
    } else {
        let _ = socket.send_to(message.as_bytes(), Path::new(&address));
    }
}
