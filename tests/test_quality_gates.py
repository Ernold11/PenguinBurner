from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_required_checks_have_unconditional_workflows() -> None:
    protection = json.loads((ROOT / ".github/branch-protection.json").read_text())
    required = {
        check["context"] for check in protection["required_status_checks"]["checks"]
    }
    assert protection["required_status_checks"]["strict"] is True
    assert protection["enforce_admins"] is True
    expected = set()
    for filename in (
        "quality.yml",
        "arch-package-build.yml",
        "fedora-package-build.yml",
        "ubuntu-package-build.yml",
        "flatpak-package-build.yml",
        "flatpak-host-python.yml",
        "flatpak-daemon-lifecycle.yml",
    ):
        workflow = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text())
        # PyYAML's YAML 1.1 parser reads the unquoted GitHub `on` key as True.
        triggers = workflow.get("on", workflow.get(True))
        assert "pull_request" in triggers
        assert not triggers["pull_request"]
        assert "merge_group" in triggers
        assert not triggers["merge_group"]
        assert "paths" not in triggers["push"]
        assert "paths-ignore" not in triggers["push"]
        for job in workflow["jobs"].values():
            assert "if" not in job
            assert not job.get("continue-on-error")
            assert all(not step.get("continue-on-error") for step in job["steps"])
            name = job["name"]
            scenarios = job.get("strategy", {}).get("matrix", {}).get("scenario")
            if isinstance(scenarios, str):
                # The last fromJSON is the supported PR matrix; the first
                # contains scheduled drift-only distro releases.
                scenarios = json.loads(
                    scenarios.rsplit("fromJSON('", 1)[1].split("')", 1)[0]
                )
            if scenarios:
                expected.update(
                    name.replace("${{ matrix.scenario }}", item) for item in scenarios
                )
            else:
                expected.add(name)
    assert required == expected


def test_quality_workflow_builds_daemon_before_running_python_tests() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/quality.yml").read_text())
    steps = workflow["jobs"]["python"]["steps"]
    commands = [step.get("run", "") for step in steps]
    build = next(
        i for i, command in enumerate(commands) if "cargo build --release" in command
    )
    tests = next(
        i for i, command in enumerate(commands) if "python -m pytest tests/" in command
    )
    assert build < tests
    assert "test -x burnerd/target/release/penguin-burnerd" in commands[tests]
    assert "scripts/check-feature-static-analysis.sh" in commands
