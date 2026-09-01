from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def test_latency_guide_documents_lutris_command_prefix() -> None:
    guide = (REPO / "docs" / "features" / "latency-fg.md").read_text(
        encoding="utf-8"
    )

    assert "Lutris" in guide
    assert "Command prefix" in guide
    assert "PENGUIN_BURNER --pb-overlay=1" in guide
    assert "WINEPREFIX" in guide


def test_latency_guide_documents_markers_without_the_overlay() -> None:
    """Turning the HUD off silently drops markers; the way back must be written
    down, including the `env` prefix Lutris needs to parse the assignment."""
    guide = (REPO / "docs" / "features" / "latency-fg.md").read_text(
        encoding="utf-8"
    )

    assert "env PB_INGAME_LATENCY=1 PENGUIN_BURNER --pb-overlay=0" in guide
