"""Tests for API-calling methods in src.pipeline (embed, fetch image, caption)."""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest
import responses

# ---------------------------------------------------------------------------
# Ensure src.pipeline can be imported even without databricks_openai installed.
# We inject a stub module into sys.modules so the top-level import succeeds.
# ---------------------------------------------------------------------------
_mock_databricks_openai = MagicMock()
sys.modules.setdefault("databricks_openai", _mock_databricks_openai)

from src.pipeline import WikiPipeline  # noqa: E402


# ---------------------------------------------------------------------------
# embed_texts
# ---------------------------------------------------------------------------


def _fake_embed_batch(_client, _model, batch: list[str]) -> list[list[float]]:
    """Return a deterministic embedding per text (index-based) so order is verifiable."""
    return [[float(hash(t) % 1000)] * 1024 for t in batch]


@patch("src.pipeline._embed_batch")
def test_embed_texts_single_batch(mock_embed):
    """Fewer than 64 texts should trigger exactly one _embed_batch call."""
    mock_embed.side_effect = _fake_embed_batch

    texts = [f"text_{i}" for i in range(10)]
    result = WikiPipeline.embed_texts(texts)

    assert mock_embed.call_count == 1
    assert len(result) == 10
    assert all(len(v) == 1024 for v in result)


@patch("src.pipeline._embed_batch")
def test_embed_texts_multiple_batches(mock_embed):
    """100 texts with batch size 64 should produce exactly 2 batch calls."""
    mock_embed.side_effect = _fake_embed_batch

    texts = [f"text_{i}" for i in range(100)]
    result = WikiPipeline.embed_texts(texts)

    assert mock_embed.call_count == 2
    assert len(result) == 100


@patch("src.pipeline._embed_batch")
def test_embed_texts_preserves_order(mock_embed):
    """Returned embeddings must correspond to the input texts in order."""
    mock_embed.side_effect = _fake_embed_batch

    texts = ["alpha", "beta", "gamma"]
    result = WikiPipeline.embed_texts(texts)

    expected = _fake_embed_batch(None, None, texts)
    assert result == expected


# ---------------------------------------------------------------------------
# fetch_image_from_mediawiki
# ---------------------------------------------------------------------------

MW_BASE = "http://wiki.test"


@responses.activate
def test_fetch_image_success():
    """Successful API lookup + image download returns raw bytes."""
    image_bytes = b"\x89PNG\r\n\x1a\nfake-image-data"

    # MediaWiki API response
    responses.add(
        responses.GET,
        f"{MW_BASE}/api.php",
        json={
            "query": {
                "pages": {
                    "123": {
                        "imageinfo": [{"url": f"{MW_BASE}/images/Example.png"}]
                    }
                }
            }
        },
        status=200,
    )
    # Actual image download
    responses.add(
        responses.GET,
        f"{MW_BASE}/images/Example.png",
        body=image_bytes,
        status=200,
    )

    result = WikiPipeline.fetch_image_from_mediawiki("Example.png", base_url=MW_BASE)

    assert result == image_bytes
    assert len(responses.calls) == 2


@responses.activate
def test_fetch_image_not_found():
    """When MediaWiki returns no imageinfo, result should be None."""
    responses.add(
        responses.GET,
        f"{MW_BASE}/api.php",
        json={
            "query": {
                "pages": {
                    "-1": {"title": "File:Missing.png", "missing": ""}
                }
            }
        },
        status=200,
    )

    result = WikiPipeline.fetch_image_from_mediawiki("Missing.png", base_url=MW_BASE)
    assert result is None


@responses.activate
def test_fetch_image_network_error():
    """Network errors should be caught and return None."""
    responses.add(
        responses.GET,
        f"{MW_BASE}/api.php",
        body=ConnectionError("network down"),
    )

    result = WikiPipeline.fetch_image_from_mediawiki("Fail.png", base_url=MW_BASE)
    assert result is None


# ---------------------------------------------------------------------------
# caption_image
# ---------------------------------------------------------------------------


@patch("src.pipeline._resize_image", side_effect=lambda b, **kw: b)
@patch("src.pipeline.DatabricksOpenAI")
def test_caption_image_returns_description(mock_oai_cls, _mock_resize):
    """Vision model response is returned as the caption string."""
    mock_client = MagicMock()
    mock_oai_cls.return_value = mock_client

    message = MagicMock()
    message.content = "A diagram showing data flow between services."
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    mock_client.chat.completions.create.return_value = response

    caption = WikiPipeline.caption_image(b"fake-png", alt_text="data flow", page_title="Arch.png")

    assert caption == "A diagram showing data flow between services."
    mock_client.chat.completions.create.assert_called_once()


@patch("src.pipeline._resize_image", side_effect=lambda b, **kw: b)
@patch("src.pipeline.DatabricksOpenAI")
def test_caption_image_fallback_on_empty(mock_oai_cls, _mock_resize):
    """When the vision model returns empty content, fall back to '[Image: alt_text]'."""
    mock_client = MagicMock()
    mock_oai_cls.return_value = mock_client

    message = MagicMock()
    message.content = ""
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    mock_client.chat.completions.create.return_value = response

    caption = WikiPipeline.caption_image(b"fake-png", alt_text="logo")

    assert caption == "[Image: logo]"
