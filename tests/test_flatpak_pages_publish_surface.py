from pathlib import Path
import subprocess


PUBLISH_SCRIPT = Path("scripts/publish-flatpak-pages.sh")
WORKFLOW = Path(".github/workflows/deploy-flatpak-pages.yml")


def test_flatpak_pages_publish_script_has_valid_bash_syntax() -> None:
    subprocess.run(("bash", "-n", str(PUBLISH_SCRIPT)), check=True)


def test_flatpak_pages_publisher_uses_release_assets_not_git_commits() -> None:
    script = PUBLISH_SCRIPT.read_text(encoding="utf-8")
    assert 'gh release upload "$tag"' in script
    assert 'gh workflow run "$WORKFLOW_FILE"' in script
    assert "git commit" not in script
    assert "git push" not in script
    assert "--migrate-ref" in script
    assert "--prepare-only" in script
    assert "--upload-only" in script
    assert "PENGUIN_BURNER_FLATPAK_BUILD_BUNDLE=1" in script
    assert '.publishedAt < \\"$target_published_at\\"' in script
    assert "Settings → Pages → Source to GitHub Actions" in script


def test_flatpak_pages_workflow_is_manual_and_minimally_privileged() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "push:" not in workflow
    assert "permissions: {}" in workflow
    assert "contents: read" in workflow
    assert "pages: write" in workflow
    assert "id-token: write" in workflow
    assert "secrets." not in workflow
    assert "gh-pages" not in workflow


def test_flatpak_pages_workflow_pins_and_validates_actions() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10" in workflow
    assert (
        "actions/configure-pages@983d7736d9b0ae728b81ab479565c72886d7745b" in workflow
    )
    assert (
        "actions/upload-pages-artifact@7b1f4a764d45c48632c6b24a0339c27f5614fb0b"
        in workflow
    )
    assert "actions/deploy-pages@d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e" in workflow
    assert "flatpak_pages_artifact.py check-tag" in workflow
    assert "flatpak_pages_artifact.py extract" in workflow
