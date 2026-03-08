"""Tests for src.ingestion — MediaWikiIngestion class."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.ingestion import MediaWikiIngestion, WikiPage


def test_wiki_page_dataclass():
    """WikiPage holds all expected fields."""
    page = WikiPage(page_id=1, page_title="Test", page_ns=0, rev_id=42, wikitext="hello")
    assert page.page_id == 1
    assert page.page_title == "Test"
    assert page.wikitext == "hello"


def test_fetch_pages_yields_wiki_pages():
    """fetch_pages yields WikiPage objects from cursor rows."""
    mock_cursor = MagicMock()
    mock_cursor.__iter__ = MagicMock(return_value=iter([
        {"page_id": 1, "page_title": "Page A", "page_ns": 0, "rev_id": 10, "wikitext": "text1"},
        {"page_id": 2, "page_title": "Page B", "page_ns": 0, "rev_id": 20, "wikitext": "text2"},
    ]))
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor

    ingestion = MediaWikiIngestion()
    pages = list(ingestion.fetch_pages(mock_conn, watermark_rev_id=0))

    assert len(pages) == 2
    assert isinstance(pages[0], WikiPage)
    assert pages[0].page_title == "Page A"
    assert pages[1].rev_id == 20


def test_fetch_pages_handles_bytea():
    """fetch_pages decodes bytes/memoryview wikitext to string."""
    mock_cursor = MagicMock()
    mock_cursor.__iter__ = MagicMock(return_value=iter([
        {"page_id": 1, "page_title": "Binary", "page_ns": 0, "rev_id": 5, "wikitext": b"hello bytes"},
    ]))
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor

    ingestion = MediaWikiIngestion()
    pages = list(ingestion.fetch_pages(mock_conn))

    assert pages[0].wikitext == "hello bytes"


def test_fetch_pages_empty_result():
    """fetch_pages yields nothing when cursor is empty."""
    mock_cursor = MagicMock()
    mock_cursor.__iter__ = MagicMock(return_value=iter([]))
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor

    ingestion = MediaWikiIngestion()
    pages = list(ingestion.fetch_pages(mock_conn))
    assert pages == []
