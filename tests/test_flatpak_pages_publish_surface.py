import subprocess
from html.parser import HTMLParser
from pathlib import Path


PUBLISH_SCRIPT = Path("scripts/publish-flatpak-pages.sh")
WORKFLOW = Path(".github/workflows/deploy-flatpak-pages.yml")
AMD_RDNA_RESEARCH_PAGE = Path("docs/pages/amd-rdna-undervolting/index.html")


class _ResearchPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.fragment_links: list[str] = []
        self.external_assets: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        if element_id := attributes.get("id"):
            self.ids.append(element_id)
        if tag == "a" and (href := attributes.get("href", "")).startswith("#"):
            self.fragment_links.append(href[1:])
        if tag in {"img", "script"} and (source := attributes.get("src")):
            self.external_assets.append(source)
        if tag == "link" and attributes.get("rel") == "stylesheet":
            self.external_assets.append(attributes.get("href", ""))


def test_flatpak_pages_publish_script_has_valid_bash_syntax() -> None:
    subprocess.run(("bash", "-n", str(PUBLISH_SCRIPT)), check=True)


def test_flatpak_pages_publisher_uses_release_assets_not_git_commits() -> None:
    script = PUBLISH_SCRIPT.read_text(encoding="utf-8")
    assert 'gh release upload "$tag"' in script
    assert 'gh workflow run "$WORKFLOW_FILE"' in script
    assert "git commit" not in script
    assert "git push" not in script
    assert "--migrate-ref" not in script
    assert "gh-pages" not in script
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


def test_flatpak_pages_workflow_adds_research_without_replacing_snapshot() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert AMD_RDNA_RESEARCH_PAGE.is_file()
    assert str(AMD_RDNA_RESEARCH_PAGE) in workflow
    assert 'target_page="site/amd-rdna-undervolting/index.html"' in workflow
    assert 'install -Dm0644 "$source_page" "$target_page"' in workflow
    assert "flatpak_pages_artifact.py validate site" in workflow
    assert 'target_page="site/index.html"' not in workflow


def test_amd_rdna_research_page_is_standalone_and_internally_linked() -> None:
    page = AMD_RDNA_RESEARCH_PAGE.read_text(encoding="utf-8")
    parser = _ResearchPageParser()
    parser.feed(page)

    assert not parser.external_assets
    assert len(parser.ids) == len(set(parser.ids))
    assert set(parser.fragment_links) <= set(parser.ids)
    assert "https://jpietek.github.io/PenguinBurner/amd-rdna-undervolting/" in page
    assert "VoltageOffsetPerZoneBoundary" in page
    assert "GlobalOffset" in page
    assert "ZoneBoundaryOffsets" in page
