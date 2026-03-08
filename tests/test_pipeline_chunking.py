"""Tests for chunking and MIME guessing (pure logic, no mocks)."""

from src.pipeline import TextChunk, WikiPipeline, _guess_mime

# A text long enough to produce multiple 512-char chunks.
LONG_TEXT = (
    "Artificial intelligence (AI) is the simulation of human intelligence "
    "processes by computer systems. These processes include learning, "
    "reasoning, and self-correction. "
) * 20  # ~5 000 chars → several chunks


# ---------------------------------------------------------------------------
# chunk_page
# ---------------------------------------------------------------------------


def test_chunk_page_splits_text():
    chunks = WikiPipeline.chunk_page(
        page_id=1, page_title="AI", page_ns=0, rev_id=100, clean_text=LONG_TEXT,
    )
    assert len(chunks) > 1
    assert all(isinstance(c, TextChunk) for c in chunks)


def test_chunk_page_preserves_metadata():
    chunks = WikiPipeline.chunk_page(
        page_id=42, page_title="Test Page", page_ns=4, rev_id=999, clean_text=LONG_TEXT,
    )
    for chunk in chunks:
        assert chunk.page_id == 42
        assert chunk.page_title == "Test Page"
        assert chunk.page_ns == 4
        assert chunk.rev_id == 999


def test_chunk_page_default_source_is_text():
    chunks = WikiPipeline.chunk_page(
        page_id=1, page_title="AI", page_ns=0, rev_id=100, clean_text=LONG_TEXT,
    )
    for chunk in chunks:
        assert chunk.chunk_source == "text"


def test_chunk_page_empty_input():
    assert WikiPipeline.chunk_page(1, "X", 0, 1, "") == []
    assert WikiPipeline.chunk_page(1, "X", 0, 1, "   ") == []
    assert WikiPipeline.chunk_page(1, "X", 0, 1, "\n\n") == []


# ---------------------------------------------------------------------------
# chunk_image_caption
# ---------------------------------------------------------------------------


def test_chunk_image_caption_tags_as_image():
    chunks = WikiPipeline.chunk_image_caption(
        page_id=1, page_title="AI", page_ns=0, rev_id=100,
        filename="diagram.png", caption="A diagram showing neural networks.",
    )
    assert len(chunks) >= 1
    for chunk in chunks:
        assert chunk.chunk_source == "image"


def test_chunk_image_caption_includes_filename():
    chunks = WikiPipeline.chunk_image_caption(
        page_id=1, page_title="AI", page_ns=0, rev_id=100,
        filename="diagram.png", caption="A diagram showing neural networks.",
    )
    assert any("diagram.png" in c.text for c in chunks)


def test_chunk_image_caption_offset():
    chunks = WikiPipeline.chunk_image_caption(
        page_id=1, page_title="AI", page_ns=0, rev_id=100,
        filename="img.png", caption="Description of the image.",
        chunk_index_offset=5,
    )
    assert chunks[0].chunk_index == 5


# ---------------------------------------------------------------------------
# _guess_mime
# ---------------------------------------------------------------------------


def test_guess_mime_known_extensions():
    assert _guess_mime("photo.jpg") == "image/jpeg"
    assert _guess_mime("photo.jpeg") == "image/jpeg"
    assert _guess_mime("icon.png") == "image/png"
    assert _guess_mime("anim.gif") == "image/gif"
    assert _guess_mime("logo.svg") == "image/svg+xml"
    assert _guess_mime("hero.webp") == "image/webp"


def test_guess_mime_fallback():
    assert _guess_mime("file.bmp") == "image/png"
    assert _guess_mime("file.tiff") == "image/png"
    assert _guess_mime("noextension") == "image/png"
