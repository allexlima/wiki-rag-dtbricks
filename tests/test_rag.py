"""Tests for src.rag — WikiRAGAgent, _llm_call, run_agent, RetrievedDoc."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from mlflow.types.responses import ResponsesAgentRequest

from src.rag import RetrievedDoc, WikiRAGAgent, _llm_call, run_agent


# ---------------------------------------------------------------------------
# 1. WikiRAGAgent initialisation
# ---------------------------------------------------------------------------


def test_agent_init():
    """_client and _conn start as None."""
    agent = WikiRAGAgent()
    assert agent._client is None
    assert agent._conn is None


# ---------------------------------------------------------------------------
# 2–3. _llm_call
# ---------------------------------------------------------------------------


def test_llm_call_returns_content(mock_openai_client):
    """_llm_call returns stripped content from the first choice."""
    mock_openai_client.chat.completions.create.return_value.choices[
        0
    ].message.content = "  hello world  "
    result = _llm_call(mock_openai_client, model="m", messages=[])
    assert result == "hello world"


def test_llm_call_handles_none_content(mock_openai_client):
    """When the model returns None content, _llm_call returns empty string."""
    mock_openai_client.chat.completions.create.return_value.choices[
        0
    ].message.content = None
    result = _llm_call(mock_openai_client, model="m", messages=[])
    assert result == ""


# ---------------------------------------------------------------------------
# 4–5. retrieve
# ---------------------------------------------------------------------------


@patch("src.rag.WikiPipeline.embed_texts", return_value=[[0.1] * 1024])
@patch("src.rag.register_vector")
def test_retrieve_returns_docs(_mock_reg, _mock_embed, mock_db_conn, mock_db_cursor):
    """retrieve() returns a list of RetrievedDoc from cursor rows."""
    mock_db_cursor.fetchall.return_value = [
        (1, "Page A", "chunk text", 0.95, "text"),
        (2, "Page B", "another chunk", 0.88, "image"),
    ]

    agent = WikiRAGAgent()
    docs = agent.retrieve(mock_db_conn, "test query", top_k=2)

    assert len(docs) == 2
    assert isinstance(docs[0], RetrievedDoc)
    assert docs[0].chunk_id == 1
    assert docs[0].page_title == "Page A"
    assert docs[0].similarity == 0.95
    assert docs[1].chunk_source == "image"


@patch("src.rag.WikiPipeline.embed_texts", side_effect=RuntimeError("boom"))
@patch("src.rag.register_vector")
def test_retrieve_handles_error(_mock_reg, _mock_embed, mock_db_conn):
    """retrieve() returns [] when an exception occurs."""
    agent = WikiRAGAgent()
    docs = agent.retrieve(mock_db_conn, "test query")
    assert docs == []


# ---------------------------------------------------------------------------
# 6–8. _load_history
# ---------------------------------------------------------------------------


def test_load_history_formats_correctly(mock_db_conn, mock_db_cursor):
    """_load_history formats rows as 'role: content' lines."""
    mock_db_cursor.fetchall.return_value = [
        ("assistant", "Sure, here you go."),
        ("user", "What is AI?"),
    ]

    agent = WikiRAGAgent()
    agent._conn = mock_db_conn

    history = agent._load_history("conv-123")
    # Rows are reversed then joined
    assert "user: What is AI?" in history
    assert "assistant: Sure, here you go." in history
    # user line should come first after reversal
    assert history.index("user:") < history.index("assistant:")


def test_load_history_empty(mock_db_conn, mock_db_cursor):
    """_load_history returns '' when no rows exist."""
    mock_db_cursor.fetchall.return_value = []

    agent = WikiRAGAgent()
    agent._conn = mock_db_conn

    assert agent._load_history("conv-456") == ""


def test_load_history_handles_error(mock_db_conn, mock_db_cursor):
    """_load_history returns '' when an exception is raised."""
    mock_db_cursor.fetchall.side_effect = RuntimeError("db down")

    agent = WikiRAGAgent()
    agent._conn = mock_db_conn

    assert agent._load_history("conv-789") == ""


# ---------------------------------------------------------------------------
# 9–10. predict (empty / whitespace input)
# ---------------------------------------------------------------------------


def _extract_text(response):
    """Extract text from a ResponsesAgentResponse output item."""
    item = response.output[0]
    # OutputItem.content is a list of content blocks; first block has .text
    content = item.content[0]
    return content.text if hasattr(content, "text") else content["text"]


def test_predict_empty_input():
    """predict() returns error response when input is empty."""
    agent = WikiRAGAgent()
    request = ResponsesAgentRequest(input=[])
    response = agent.predict(request)

    text = _extract_text(response)
    assert "provide a question" in text.lower()


def test_predict_empty_question():
    """predict() returns error response when question is whitespace."""
    agent = WikiRAGAgent()
    request = ResponsesAgentRequest(input=[{"role": "user", "content": "   "}])

    with patch.object(agent, "_build_graph") as mock_graph, \
         patch.object(agent, "_load_history", return_value=""), \
         patch.object(agent, "_save_exchange"), \
         patch.object(agent, "_get_conn", return_value=MagicMock()):
        response = agent.predict(request)
        mock_graph.assert_not_called()

    text = _extract_text(response)
    assert "non-empty" in text.lower()


# ---------------------------------------------------------------------------
# 11. predict_stream
# ---------------------------------------------------------------------------


def _make_stream_events(documents, generation):
    """Build a mock graph.stream() return value (node-level updates)."""
    return iter([
        {"retrieve": {"documents": documents}},
        {"grade_documents": {"documents": documents}},
        {"generate": {"generation": generation}},
    ])


def test_predict_stream_yields_events():
    """predict_stream uses graph.stream and yields output events."""
    agent = WikiRAGAgent()

    mock_graph = MagicMock()
    mock_graph.stream.return_value = _make_stream_events(
        documents=[{"title": "P", "similarity": 0.9,
                     "source": "text", "text": "..."}],
        generation="streamed answer",
    )

    with patch.object(agent, "_build_graph", return_value=mock_graph), \
         patch.object(agent, "_load_history", return_value=""), \
         patch.object(agent, "_save_exchange"), \
         patch.object(agent, "_get_conn", return_value=MagicMock()):
        request = ResponsesAgentRequest(
            input=[{"role": "user", "content": "hi"}],
        )
        events = list(agent.predict_stream(request))

    assert len(events) == 1
    assert events[0].type == "response.output_item.done"
    mock_graph.stream.assert_called_once()


# ---------------------------------------------------------------------------
# 12. run_agent convenience function
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Additional coverage: _save_exchange, predict happy path
# ---------------------------------------------------------------------------


def test_save_exchange_persists(mock_db_conn, mock_db_cursor):
    """_save_exchange executes INSERT statements and commits."""
    agent = WikiRAGAgent()
    agent._conn = mock_db_conn

    agent._save_exchange("conv-1", "user@test.com", "Q?", "A.", [{"title": "P"}])

    # Should have 3 execute calls: upsert conversation, insert user msg, insert assistant msg
    assert mock_db_cursor.execute.call_count == 3
    mock_db_conn.commit.assert_called_once()


def test_save_exchange_handles_error(mock_db_conn, mock_db_cursor):
    """_save_exchange swallows errors silently."""
    mock_db_cursor.execute.side_effect = RuntimeError("db error")
    agent = WikiRAGAgent()
    agent._conn = mock_db_conn

    # Should not raise
    agent._save_exchange("conv-1", "user", "Q?", "A.", [])


def test_predict_full_pipeline():
    """predict() orchestrates graph.stream, memory, and returns response."""
    agent = WikiRAGAgent()

    mock_graph = MagicMock()
    mock_graph.stream.return_value = _make_stream_events(
        documents=[{"title": "Page A", "similarity": 0.9,
                     "source": "text", "text": "..."}],
        generation="The answer is here.",
    )

    with patch.object(agent, "_build_graph", return_value=mock_graph), \
         patch.object(agent, "_load_history", return_value=""), \
         patch.object(agent, "_save_exchange") as mock_save, \
         patch.object(agent, "_get_conn", return_value=MagicMock()):

        request = ResponsesAgentRequest(
            input=[{"role": "user", "content": "What is AI?"}]
        )
        response = agent.predict(request)

        text = _extract_text(response)
        assert "answer is here" in text.lower()
        mock_graph.stream.assert_called_once()
        mock_save.assert_called_once()


# ---------------------------------------------------------------------------
# 12. run_agent convenience function
# ---------------------------------------------------------------------------


def test_run_agent_convenience():
    """run_agent() returns a dict with 'answer' and 'conversation_id'."""
    mock_graph = MagicMock()
    mock_graph.stream.return_value = _make_stream_events(
        documents=[],
        generation="The answer is 42.",
    )

    with patch.object(WikiRAGAgent, "_build_graph", return_value=mock_graph), \
         patch.object(WikiRAGAgent, "_load_history", return_value=""), \
         patch.object(WikiRAGAgent, "_save_exchange"), \
         patch.object(WikiRAGAgent, "_get_conn", return_value=MagicMock()):

        result = run_agent("What is the meaning?", thread_id="t-1")

    assert "42" in result["answer"]
    assert result["conversation_id"] == "t-1"
