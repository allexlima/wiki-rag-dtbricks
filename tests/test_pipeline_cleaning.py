"""Tests for wikitext cleaning and image extraction (pure logic, no mocks)."""

from src.pipeline import ImageRef, WikiPipeline

SAMPLE_WIKITEXT = """
== Introduction ==

This is about '''artificial intelligence'''.

[[File:AI_diagram.png|thumb|300px|Overview of AI concepts]]

AI is the simulation of human intelligence.

{{citation needed}}

[[Image:Turing.jpg|left|150px|Alan Turing portrait]]

Alan Turing proposed the Turing test.
"""


# ---------------------------------------------------------------------------
# clean_wikitext
# ---------------------------------------------------------------------------


def test_clean_wikitext_strips_markup():
    result = WikiPipeline.clean_wikitext(SAMPLE_WIKITEXT)
    # Headings, bold markers, and link brackets should be gone
    assert "==" not in result
    assert "'''" not in result
    assert "[[" not in result
    assert "]]" not in result
    # Plain content survives
    assert "artificial intelligence" in result
    assert "AI is the simulation" in result


def test_clean_wikitext_removes_skip_templates():
    wikitext = (
        "Some text.\n\n"
        "{{stub}}\n"
        "{{citation needed}}\n"
        "{{infobox|name=Test}}\n"
        "More text."
    )
    result = WikiPipeline.clean_wikitext(wikitext)
    assert "stub" not in result.lower()
    assert "citation needed" not in result.lower()
    assert "infobox" not in result.lower()
    assert "Some text." in result
    assert "More text." in result


def test_clean_wikitext_collapses_blank_lines():
    wikitext = "Line one.\n\n\n\n\nLine two.\n\n\n\nLine three."
    result = WikiPipeline.clean_wikitext(wikitext)
    # No run of more than one blank line (two consecutive newlines)
    assert "\n\n\n" not in result
    assert "Line one." in result
    assert "Line two." in result
    assert "Line three." in result


def test_clean_wikitext_empty_input():
    assert WikiPipeline.clean_wikitext("") == ""
    assert WikiPipeline.clean_wikitext("   ") == ""
    assert WikiPipeline.clean_wikitext("\n\n") == ""


# ---------------------------------------------------------------------------
# extract_image_refs
# ---------------------------------------------------------------------------


def test_extract_image_refs_finds_files():
    refs = WikiPipeline.extract_image_refs(SAMPLE_WIKITEXT)
    filenames = [r.filename for r in refs]
    assert "AI_diagram.png" in filenames
    assert "Turing.jpg" in filenames
    assert len(refs) == 2
    assert all(isinstance(r, ImageRef) for r in refs)


def test_extract_image_refs_strips_mw_options():
    refs = WikiPipeline.extract_image_refs(SAMPLE_WIKITEXT)
    by_name = {r.filename: r for r in refs}

    ai_ref = by_name["AI_diagram.png"]
    # "thumb" and "300px" should be stripped; "Overview of AI concepts" kept
    assert "thumb" not in ai_ref.alt_text.lower()
    assert "300px" not in ai_ref.alt_text
    assert "Overview of AI concepts" in ai_ref.alt_text

    turing_ref = by_name["Turing.jpg"]
    # "left" and "150px" should be stripped; portrait text kept
    assert "left" not in turing_ref.alt_text.split()
    assert "150px" not in turing_ref.alt_text
    assert "Alan Turing portrait" in turing_ref.alt_text


def test_extract_image_refs_empty_input():
    assert WikiPipeline.extract_image_refs("") == []
    assert WikiPipeline.extract_image_refs("   ") == []


def test_extract_image_refs_no_images():
    wikitext = "== Heading ==\nJust plain text with '''bold''' and [[internal link]]."
    refs = WikiPipeline.extract_image_refs(wikitext)
    assert refs == []
