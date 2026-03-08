"""Shared fixtures for the wiki-rag test suite."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Wikitext samples
# ---------------------------------------------------------------------------

SIMPLE_WIKITEXT = """\
== Introduction ==

This is a simple wiki page about '''artificial intelligence'''.

[[File:AI_diagram.png|thumb|300px|Overview of AI concepts]]

AI is the simulation of human intelligence by machines.

== History ==

The term was coined in 1956 at a conference at Dartmouth College.

{{citation needed}}

[[Image:Turing.jpg|left|150px|Alan Turing portrait]]

Alan Turing proposed the Turing test in 1950.
"""

EMPTY_WIKITEXT = ""
WIKITEXT_NO_IMAGES = "== Hello ==\nThis page has no images.\n{{stub}}"
WIKITEXT_ONLY_TEMPLATES = "{{infobox}}\n{{navbox}}\n{{reflist}}"


@pytest.fixture
def simple_wikitext():
    """Sample wikitext with images, templates, and formatting."""
    return SIMPLE_WIKITEXT


@pytest.fixture
def mock_openai_client():
    """Mock DatabricksOpenAI client that returns configurable responses."""
    client = MagicMock()

    # Default embedding response
    embedding_item = MagicMock()
    embedding_item.embedding = [0.1] * 1024
    embedding_response = MagicMock()
    embedding_response.data = [embedding_item]
    client.embeddings.create.return_value = embedding_response

    # Default chat response
    message = MagicMock()
    message.content = "This is a test response."
    choice = MagicMock()
    choice.message = message
    chat_response = MagicMock()
    chat_response.choices = [choice]
    client.chat.completions.create.return_value = chat_response

    return client


@pytest.fixture
def mock_db_cursor():
    """Mock psycopg2 cursor with configurable fetchall/fetchone."""
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    cursor.fetchone.return_value = None
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    return cursor


@pytest.fixture
def mock_db_conn(mock_db_cursor):
    """Mock psycopg2 connection that yields the mock cursor."""
    conn = MagicMock()
    conn.closed = False
    conn.cursor.return_value = mock_db_cursor
    return conn
