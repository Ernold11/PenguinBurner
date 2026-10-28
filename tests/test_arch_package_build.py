from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("scenario", "failures", "syncs", "exit_code"),
    [
        ("vanilla", 0, 0, 77),
        ("cachyos-shelly", 0, 1, 77),
        ("cachyos-shelly", 1, 2, 77),
        ("cachyos-shelly", 6, 6, 1),
    ],
)
def test_signed_database_sync_retries_before_upgrade(
    tmp_path: Path, scenario: str, failures: int, syncs: int, exit_code: int
) -> None:
    """Run the real container script with only its engine and pacman stubbed.

    Stop deliberately at the upgrade boundary: persistent sync failures must
    never reach it, and retries must retain normal signature verification.
    """
    trace = tmp_path / "calls.json"
    trace.write_text("[]")
    mirror_dir = tmp_path / "mirrors"
    mirror_dir.mkdir()
    for suffix, arch in (("", "$arch"), ("-v3", "$arch_v3")):
        (mirror_dir / f"cachyos{suffix}-mirrorlist").write_text(
            "# Server = https://disabled.invalid/repo/"
            + arch
            + "/$repo\n"
            + "".join(
                f"Server = https://mirror{i}.invalid/repo/{arch}/$repo\n"
                for i in range(8)
            )
        )
    sync_dir = tmp_path / "sync"
    sync_dir.mkdir()
    (sync_dir / "core.db").write_text("keep Arch database")
    pacman_conf = tmp_path / "pacman.conf"
    pacman_conf.write_text(
        "[options]\nSigLevel = Required DatabaseOptional\n[cachyos]\n"
    )
    programs = {
        "engine": """
import os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
assert any(arg.endswith(":/work:ro,Z") for arg in args)
env = dict(os.environ)
env["SCENARIO"] = next(arg.split("=", 1)[1] for arg in args if arg.startswith("SCENARIO="))
work = next(arg.removesuffix(":/work:ro,Z") for arg in args if arg.endswith(":/work:ro,Z"))
assert Path(work, "refresh-cachyos.sh").is_file()
args[-1] = args[-1].replace("/etc/pacman.conf", env["PACMAN_CONF"])
args[-1] = args[-1].replace("bash /work/refresh-cachyos.sh", f'bash {work}/refresh-cachyos.sh "{env["MIRROR_DIR"]}" "{env["SYNC_DIR"]}"')
raise SystemExit(subprocess.call(args[args.index("bash"):], env=env))
""",
        "pacman": """
import json, os, sys
from pathlib import Path
trace = Path(os.environ["PACMAN_TRACE"])
calls = json.loads(trace.read_text())
calls.append(sys.argv[1:])
trace.write_text(json.dumps(calls))
if sys.argv[1] in ("-Syu", "-Su"):
    raise SystemExit(77)
assert sys.argv[1:] == ["-Syy", "--noconfirm"]
assert "SigLevel = Required DatabaseRequired" in Path(os.environ["PACMAN_CONF"]).read_text()
mirrors = Path(os.environ["MIRROR_DIR"])
normal = (mirrors / "cachyos-mirrorlist").read_text()
v3 = (mirrors / "cachyos-v3-mirrorlist").read_text()
assert normal.replace("$arch", "$arch_v3") == v3
assert f"mirror{len(calls)-1}.invalid" in normal
sync = Path(os.environ["SYNC_DIR"])
assert not (sync / "cachyos.db").exists()
assert not (sync / "cachyos.db.sig").exists()
assert (sync / "core.db").read_text() == "keep Arch database"
(sync / "cachyos.db").write_text("mismatched database")
(sync / "cachyos.db.sig").write_text("invalid signature")
raise SystemExit(1 if len(calls) <= int(os.environ["SYNC_FAILURES"]) else 0)
""",
        "sleep": "pass\n",
    }
    for name, body in programs.items():
        executable = tmp_path / name
        executable.write_text(f"#!{sys.executable}\n{body}")
        executable.chmod(0o755)

    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["bash", str(root / "scripts/check-arch-package-build.sh"), scenario],
        cwd=root,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}",
            "PENGUIN_BURNER_CONTAINER_ENGINE": str(tmp_path / "engine"),
            "PACMAN_TRACE": str(trace),
            "SYNC_FAILURES": str(failures),
            "MIRROR_DIR": str(mirror_dir),
            "SYNC_DIR": str(sync_dir),
            "PACMAN_CONF": str(pacman_conf),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == exit_code, result.stdout + result.stderr
    expected = [["-Syy", "--noconfirm"]] * syncs
    if exit_code == 77:
        expected.append(
            ["-Su" if scenario == "cachyos-shelly" else "-Syu", "--noconfirm"]
        )
    assert json.loads(trace.read_text()) == expected
    if scenario == "cachyos-shelly" and exit_code == 77:
        mirrors = (mirror_dir / "cachyos-mirrorlist").read_text().splitlines()
        assert mirrors[0] == f"Server = https://mirror{failures}.invalid/repo/$arch/$repo"
        assert len(mirrors) == 8
        assert len(set(mirrors)) == 8
        assert all("disabled.invalid" not in mirror for mirror in mirrors)
