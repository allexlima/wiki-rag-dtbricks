"""
Wiki RAG Agent — ResponsesAgent wrapping a LangGraph agentic RAG flow.

Single source of truth: used by both notebook testing and model serving.
Deployment: ``mlflow.pyfunc.log_model(python_model="src/rag.py")``

Flow: retrieve → grade_documents → (rewrite_query loop) → generate
Memory: conversation history persisted in Lakebase ``wiki_rag.messages``.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Generator, TypedDict

import mlflow
import psycopg2
from databricks_openai import DatabricksOpenAI
from langgraph.graph import END, StateGraph
from mlflow.pyfunc import ResponsesAgent
from mlflow.types.responses import (
    ResponsesAgentRequest,
    ResponsesAgentResponse,
    ResponsesAgentStreamEvent,
)
from pgvector.psycopg2 import register_vector
from tenacity import retry, stop_after_attempt, wait_exponential

from src.pipeline import WikiPipeline

log = logging.getLogger(__name__)

mlflow.langchain.autolog()

# ---------------------------------------------------------------------------
# Configuration (overridable via env vars in serving context)
# ---------------------------------------------------------------------------
LLM_MODEL = os.environ.get("LLM_MODEL", "databricks-meta-llama-3-3-70b-instruct")
MAX_REWRITES = 2
LLM_TIMEOUT = 60  # seconds
MAX_HISTORY_TURNS = 5  # previous exchanges to include as context


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass
class RetrievedDoc:
    """A document chunk retrieved from pgvector similarity search.

    Attributes:
        chunk_source: ``"text"`` for regular wiki content,
            ``"image"`` for vision-LLM-generated captions.
    """

    chunk_id: int
    page_title: str
    chunk_text: str
    similarity: float
    chunk_source: str = "text"


# ---------------------------------------------------------------------------
# Internal LangGraph state
# ---------------------------------------------------------------------------


class _RAGState(TypedDict):
    question: str
    documents: list[dict]
    generation: str
    rewrite_count: int
    conversation_history: str


# ---------------------------------------------------------------------------
# LLM call wrapper with retry + timeout
# ---------------------------------------------------------------------------


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=30))
def _llm_call(client: DatabricksOpenAI, **kwargs) -> str:
    """Make an LLM call with retry and timeout.

    Args:
        client: DatabricksOpenAI client instance.
        **kwargs: Forwarded to ``client.chat.completions.create()``.

    Returns:
        The stripped content string from the first choice.
    """
    kwargs.setdefault("timeout", LLM_TIMEOUT)
    response = client.chat.completions.create(**kwargs)
    content = response.choices[0].message.content
    if content is None:
        return ""
    return content.strip()


# ---------------------------------------------------------------------------
# WikiRAGAgent
# ---------------------------------------------------------------------------


class WikiRAGAgent(ResponsesAgent):
    """Agentic RAG over a MediaWiki knowledge base stored in Lakebase/pgvector.

    Implements the MLflow 3 ``ResponsesAgent`` interface so it can be deployed
    directly as a Model Serving endpoint via *models from code*.

    The agent runs a LangGraph ``StateGraph`` with four nodes:

    1. **retrieve** — embed query and search pgvector for top-k chunks.
    2. **grade_documents** — LLM relevance judgment on each chunk.
    3. **rewrite_query** — LLM rewrites the query if no relevant docs found.
    4. **generate** — LLM produces the answer citing source pages.

    Multi-turn conversation history is persisted in Lakebase
    (``wiki_rag.conversations`` / ``wiki_rag.messages``).
    """

    def __init__(self) -> None:
        """Initialise with lazy clients (created on first use)."""
        self._client: DatabricksOpenAI | None = None
        self._conn = None

    @property
    def client(self) -> DatabricksOpenAI:
        """Lazy-initialised DatabricksOpenAI client."""
        if self._client is None:
            self._client = DatabricksOpenAI()
        return self._client

    def _get_conn(self) -> psycopg2.extensions.connection:
        """Return a live psycopg2 connection, reconnecting if needed.

        Uses :func:`src.config.get_lakebase_conn` which supports both
        password auth (serving) and OAuth (notebooks).
        """
        if self._conn is None or self._conn.closed:
            from src.config import get_lakebase_conn

            self._conn = get_lakebase_conn()
        return self._conn

    # --- Retriever --------------------------------------------------------

    def retrieve(
        self,
        conn: psycopg2.extensions.connection,
        query: str,
        top_k: int = 5,
    ) -> list[RetrievedDoc]:
        """Embed *query* and search pgvector for the *top_k* most similar chunks.

        Args:
            conn: A psycopg2 connection to Lakebase.
            query: The user's natural-language question.
            top_k: Number of results to return.

        Returns:
            A list of :class:`RetrievedDoc` ordered by descending similarity.
            Returns an empty list on any failure.
        """
        try:
            register_vector(conn)
            query_embedding = WikiPipeline.embed_texts([query])[0]

            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT c.chunk_id, c.page_title, c.chunk_text,
                           1 - (e.embedding <=> %s::vector) AS similarity,
                           COALESCE(c.chunk_source, 'text') AS chunk_source
                    FROM wiki_rag.wiki_embeddings e
                    JOIN wiki_rag.wiki_chunks c ON c.chunk_id = e.chunk_id
                    ORDER BY e.embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (query_embedding, query_embedding, top_k),
                )
                rows = cur.fetchall()

            docs = [
                RetrievedDoc(
                    chunk_id=r[0], page_title=r[1], chunk_text=r[2],
                    similarity=r[3], chunk_source=r[4],
                )
                for r in rows
            ]
            log.info(
                "Retrieved %d docs for query (top similarity: %.3f)",
                len(docs), docs[0].similarity if docs else 0.0,
            )
            return docs

        except Exception:
            log.exception("Retrieval failed for query: %s", query[:100])
            return []

    # --- Conversation memory (Lakebase) -----------------------------------

    def _load_history(self, conversation_id: str) -> str:
        """Load recent conversation history from Lakebase.

        Args:
            conversation_id: UUID identifying the conversation thread.

        Returns:
            Formatted previous turns as ``"role: content"`` lines,
            or an empty string if unavailable.
        """
        try:
            conn = self._get_conn()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT role, content
                    FROM wiki_rag.messages
                    WHERE conversation_id = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (conversation_id, MAX_HISTORY_TURNS * 2),
                )
                rows = cur.fetchall()

            if not rows:
                return ""

            rows.reverse()
            return "\n".join(f"{role}: {content}" for role, content in rows)

        except Exception:
            log.warning("Failed to load conversation history", exc_info=True)
            return ""

    def _save_exchange(
        self,
        conversation_id: str,
        user_id: str,
        question: str,
        answer: str,
        sources: list[dict],
    ) -> None:
        """Persist the user question and assistant answer to Lakebase.

        Creates the conversation record on first exchange (upsert),
        then appends both messages atomically.

        Args:
            conversation_id: UUID for the conversation thread.
            user_id: Identifier of the user (email or ``"anonymous"``).
            question: The user's original question.
            answer: The generated answer text.
            sources: List of source dicts (``title``, ``similarity``).
        """
        try:
            conn = self._get_conn()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO wiki_rag.conversations (conversation_id, user_id)
                    VALUES (%s, %s)
                    ON CONFLICT (conversation_id) DO UPDATE SET updated_at = now()
                    """,
                    (conversation_id, user_id),
                )
                cur.execute(
                    """
                    INSERT INTO wiki_rag.messages (conversation_id, role, content)
                    VALUES (%s, 'user', %s)
                    """,
                    (conversation_id, question),
                )
                cur.execute(
                    """
                    INSERT INTO wiki_rag.messages (conversation_id, role, content, sources)
                    VALUES (%s, 'assistant', %s, %s)
                    """,
                    (conversation_id, answer, json.dumps(sources)),
                )
            conn.commit()
        except Exception:
            log.warning("Failed to save conversation exchange", exc_info=True)

    # --- LangGraph RAG pipeline -------------------------------------------

    def _build_graph(self):
        """Build and compile the LangGraph RAG agent.

        Returns:
            A compiled ``StateGraph`` ready for ``.invoke()`` or ``.stream()``.
        """
        client = self.client

        def retrieve_node(state: _RAGState) -> dict:
            conn = self._get_conn()
            docs = self.retrieve(conn, state["question"], top_k=5)
            return {
                "documents": [
                    {
                        "chunk_id": d.chunk_id,
                        "title": d.page_title,
                        "text": d.chunk_text,
                        "similarity": d.similarity,
                        "source": d.chunk_source,
                    }
                    for d in docs
                ]
            }

        def grade_documents_node(state: _RAGState) -> dict:
            question = state["question"]
            docs = state["documents"]
            if not docs:
                return {"documents": []}

            relevant: list[dict] = []
            for doc in docs:
                grade = _llm_call(
                    client,
                    model=LLM_MODEL,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You are a relevance grader. Given a question and a document, "
                                "respond with ONLY 'yes' if the document is relevant to answering "
                                "the question, or 'no' if it is not."
                            ),
                        },
                        {
                            "role": "user",
                            "content": f"Question: {question}\n\nDocument: {doc['text']}",
                        },
                    ],
                    max_tokens=3,
                    temperature=0,
                )
                if grade.lower().startswith("yes"):
                    relevant.append(doc)

            log.info("Graded %d docs → %d relevant", len(docs), len(relevant))
            return {"documents": relevant}

        def rewrite_query_node(state: _RAGState) -> dict:
            new_question = _llm_call(
                client,
                model=LLM_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a query rewriter. Rewrite the following question to be more "
                            "specific and likely to retrieve relevant wiki documents. "
                            "Return ONLY the rewritten question."
                        ),
                    },
                    {"role": "user", "content": state["question"]},
                ],
                max_tokens=128,
                temperature=0.3,
            )
            log.info("Rewrote query: %s → %s", state["question"][:60], new_question[:60])
            return {
                "question": new_question,
                "rewrite_count": state.get("rewrite_count", 0) + 1,
            }

        def generate_node(state: _RAGState) -> dict:
            docs = state["documents"]

            def _format_doc(d: dict) -> str:
                label = (
                    f"[{d['title']}] (image description)"
                    if d.get("source") == "image"
                    else f"[{d['title']}]"
                )
                return f"{label}\n{d['text']}"

            context = "\n\n".join(_format_doc(d) for d in docs)

            history = state.get("conversation_history", "")
            history_block = f"\n\nPrevious conversation:\n{history}" if history else ""

            answer = _llm_call(
                client,
                model=LLM_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a helpful wiki assistant. Answer the question using ONLY "
                            "the provided context. Cite the source page titles in your answer. "
                            "If the context doesn't contain enough information, say so."
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"Context:\n{context}{history_block}\n\nQuestion: {state['question']}",
                    },
                ],
                max_tokens=1024,
                temperature=0.1,
            )
            return {"generation": answer}

        def should_rewrite(state: _RAGState) -> str:
            if state["documents"]:
                return "generate"
            if state.get("rewrite_count", 0) >= MAX_REWRITES:
                return "generate"
            return "rewrite_query"

        graph = StateGraph(_RAGState)
        graph.add_node("retrieve", retrieve_node)
        graph.add_node("grade_documents", grade_documents_node)
        graph.add_node("rewrite_query", rewrite_query_node)
        graph.add_node("generate", generate_node)

        graph.set_entry_point("retrieve")
        graph.add_edge("retrieve", "grade_documents")
        graph.add_conditional_edges("grade_documents", should_rewrite)
        graph.add_edge("rewrite_query", "retrieve")
        graph.add_edge("generate", END)

        return graph.compile()

    # --- ResponsesAgent interface -----------------------------------------

    def predict(self, request: ResponsesAgentRequest) -> ResponsesAgentResponse:
        """Run the full RAG pipeline and return a structured response.

        Delegates to :meth:`predict_stream` and collects all output items,
        following the Agent Bricks pattern where ``predict_stream`` is the
        primary implementation.

        Args:
            request: An MLflow ``ResponsesAgentRequest`` with at least
                one user message in ``request.input``.

        Returns:
            A ``ResponsesAgentResponse`` containing the answer text
            and appended source citations.
        """
        outputs = [
            event.item
            for event in self.predict_stream(request)
            if event.type == "response.output_item.done"
        ]
        return ResponsesAgentResponse(output=outputs)

    def predict_stream(
        self, request: ResponsesAgentRequest,
    ) -> Generator[ResponsesAgentStreamEvent, None, None]:
        """Stream the RAG pipeline using LangGraph node-level streaming.

        Runs the graph with ``stream_mode="updates"`` so each node
        (retrieve, grade, rewrite, generate) streams its output as it
        completes. The final answer is yielded as a stream event.

        Args:
            request: An MLflow ``ResponsesAgentRequest``.

        Yields:
            ``ResponsesAgentStreamEvent`` objects for each output item.
        """
        if not request.input:
            yield ResponsesAgentStreamEvent(
                type="response.output_item.done",
                item=self.create_text_output_item(
                    text="Please provide a question.",
                    id="msg_err",
                ),
            )
            return

        messages = [
            {"role": m.role, "content": m.content}
            for m in request.input
        ]
        question = messages[-1].get("content", "").strip()

        if not question:
            yield ResponsesAgentStreamEvent(
                type="response.output_item.done",
                item=self.create_text_output_item(
                    text="Please provide a non-empty question.",
                    id="msg_err",
                ),
            )
            return

        conv_id = None
        user_id = "anonymous"
        if request.context:
            conv_id = getattr(
                request.context, "conversation_id", None,
            )
            user_id = (
                getattr(request.context, "user_id", None)
                or "anonymous"
            )
        if not conv_id:
            conv_id = str(uuid.uuid4())

        log.info(
            "Processing: %s (conv=%s)",
            question[:80], conv_id[:8],
        )

        history = self._load_history(conv_id)

        graph = self._build_graph()
        final_result: dict = {}
        for event in graph.stream(
            {
                "question": question,
                "documents": [],
                "generation": "",
                "rewrite_count": 0,
                "conversation_history": history,
            },
            stream_mode="updates",
        ):
            for node_output in event.values():
                final_result.update(node_output)

        answer = final_result.get(
            "generation", "I could not generate an answer.",
        )
        sources = [
            {
                "title": d["title"],
                "similarity": round(d["similarity"], 4),
            }
            for d in final_result.get("documents", [])
        ]

        self._save_exchange(
            conv_id, user_id, question, answer, sources,
        )

        response_text = answer
        if sources:
            source_list = ", ".join(
                s["title"] for s in sources
            )
            response_text += f"\n\n**Sources:** {source_list}"

        yield ResponsesAgentStreamEvent(
            type="response.output_item.done",
            item=self.create_text_output_item(
                text=response_text, id="msg_1",
            ),
        )


# ---------------------------------------------------------------------------
# Convenience function for notebook testing
# ---------------------------------------------------------------------------


def run_agent(question: str, thread_id: str | None = None) -> dict:
    """Run the RAG agent directly (for notebook testing).

    Args:
        question: The user's question.
        thread_id: Optional conversation ID for multi-turn memory.

    Returns:
        A dict with ``"answer"`` and ``"conversation_id"`` keys.
    """
    from mlflow.types.responses import ChatContext

    agent = WikiRAGAgent()
    conv_id = thread_id or str(uuid.uuid4())
    request = ResponsesAgentRequest(
        input=[{"role": "user", "content": question}],
        context=ChatContext(conversation_id=conv_id, user_id="notebook_user"),
    )
    response = agent.predict(request)

    answer = ""
    for item in response.output:
        if hasattr(item, "content") and item.content:
            block = item.content[0]
            answer = block.text if hasattr(block, "text") else str(block)
            break

    return {"answer": answer, "conversation_id": conv_id}


# ---------------------------------------------------------------------------
# MLflow model declaration (for "models from code" pattern)
# ---------------------------------------------------------------------------
AGENT = WikiRAGAgent()
mlflow.models.set_model(AGENT)
