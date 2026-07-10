from __future__ import annotations

import ast
from pathlib import Path


def test_q2rtx_has_one_implementation_tree() -> None:
    assert Path("stability/q2rtx").is_dir()
    assert not Path("auto_uv/q2rtx").exists()
    assert not Path("runtime/stability_test").exists()


def test_generic_q2rtx_does_not_depend_on_auto_uv_or_ui() -> None:
    imported_roots = _imported_roots(Path("stability/q2rtx"))

    assert "auto_uv" not in imported_roots
    assert "ui" not in imported_roots


def test_auto_uv_probe_policy_does_not_depend_on_ui() -> None:
    assert "ui" not in _imported_roots(Path("auto_uv/probes"))


def _imported_roots(package_dir: Path) -> set[str]:
    roots: set[str] = set()
    for path in package_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.partition(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.partition(".")[0])
    return roots
