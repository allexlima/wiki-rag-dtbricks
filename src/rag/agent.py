"""
Wiki RAG Agent — ResponsesAgent wrapping a LangGraph agentic RAG flow.

Single source of truth: used by both notebook testing and model serving.
Deployment: mlflow.pyfunc.log_model(python_model="src/rag/agent.py")

Flow: retrieve → grade_documents → (rewrite_query loop) → generate
Memory: conversation history persisted in Lakebase wiki_rag.messages table.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Generator, TypedDict

import mlflow
from databricks_openai import DatabricksOpenAI
from langgraph.graph import END, StateGraph
from mlflow.pyfunc import ResponsesAgent
from mlflow.types.responses import (
    ResponsesAgentRequest,
    ResponsesAgentResponse,
    ResponsesAgentStreamEvent,
)
from tenacity import retry, stop_after_attempt, wait_exponential

from src.rag.retriever import RetrievedDoc, retrieve

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (overridable via env vars in serving context)
# ---------------------------------------------------------------------------
LLM_MODEL = os.environ.get("LLM_MODEL", "databricks-meta-llama-3-3-70b-instruct")
MAX_REWRITES = 2
LLM_TIMEOUT = 60  # seconds
MAX_HISTORY_TURNS = 5  # previous exchanges to include as context


# ---------------------------------------------------------------------------
# Internal LangGraph state (custom RAG pipeline, not message-based)
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
    """Make an LLM call with retry and timeout. Returns content string."""
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
    """Agentic RAG over a MediaWiki knowledge base stored in Lakebase/pgvector."""

    def __init__(self):
        self._client: DatabricksOpenAI | None = None
        self._conn = None

    @property
    def client(self) -> DatabricksOpenAI:
        if self._client is None:
            self._client = DatabricksOpenAI()
        return self._client

    def _get_conn(self):
        """Lazy psycopg2 connection via src.config."""
        if self._conn is None or self._conn.closed:
            from src.config import get_lakebase_conn

            self._conn = get_lakebase_conn()
        return self._conn

    # --- Conversation memory (Lakebase) -----------------------------------

    def _load_history(self, conversation_id: str) -> str:
        """Load recent conversation history from Lakebase as formatted context."""
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

            rows.reverse()  # chronological order
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
        """Persist the user question and assistant answer to Lakebase."""
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
        """Build and compile the LangGraph RAG agent."""
        client = self.client

        def retrieve_node(state: _RAGState) -> dict:
            conn = self._get_conn()
            docs = retrieve(conn, state["question"], top_k=5)
            return {
                "documents": [
                    {
                        "chunk_id": d.chunk_id,
                        "title": d.page_title,
                        "text": d.chunk_text,
                        "similarity": d.similarity,
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
            context = "\n\n".join(f"[{d['title']}]\n{d['text']}" for d in docs)

            history = state.get("conversation_history", "")
            history_block = (
                f"\n\nPrevious conversation:\n{history}" if history else ""
            )

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
        """Run the RAG pipeline and return a structured response."""
        if not request.input:
            return ResponsesAgentResponse(
                output=[self.create_text_output_item(text="Please provide a question.", id="msg_err")]
            )

        messages = [{"role": m.role, "content": m.content} for m in request.input]
        question = messages[-1].get("content", "").strip()

        if not question:
            return ResponsesAgentResponse(
                output=[self.create_text_output_item(text="Please provide a non-empty question.", id="msg_err")]
            )

        # Context from request
        conv_id = None
        user_id = "anonymous"
        if request.context:
            conv_id = getattr(request.context, "conversation_id", None)
            user_id = getattr(request.context, "user_id", None) or "anonymous"
        if not conv_id:
            conv_id = str(uuid.uuid4())

        log.info("Processing: %s (conv=%s)", question[:80], conv_id[:8])

        # Load conversation history for multi-turn context
        history = self._load_history(conv_id)

        # Run the LangGraph RAG pipeline
        graph = self._build_graph()
        result = graph.invoke({
            "question": question,
            "documents": [],
            "generation": "",
            "rewrite_count": 0,
            "conversation_history": history,
        })

        answer = result.get("generation", "I could not generate an answer.")
        sources = [
            {"title": d["title"], "similarity": round(d["similarity"], 4)}
            for d in result.get("documents", [])
        ]

        # Persist the exchange
        self._save_exchange(conv_id, user_id, question, answer, sources)

        # Build response with sources appended
        response_text = answer
        if sources:
            source_list = ", ".join(s["title"] for s in sources)
            response_text += f"\n\n**Sources:** {source_list}"

        return ResponsesAgentResponse(
            output=[self.create_text_output_item(text=response_text, id="msg_1")]
        )

    def predict_stream(
        self, request: ResponsesAgentRequest,
    ) -> Generator[ResponsesAgentStreamEvent, None, None]:
        """Streaming predict — delegates to non-streaming for simplicity."""
        result = self.predict(request)
        for item in result.output:
            yield ResponsesAgentStreamEvent(type="response.output_item.done", item=item)


# ---------------------------------------------------------------------------
# Convenience function for notebook testing
# ---------------------------------------------------------------------------
def run_agent(question: str, thread_id: str | None = None) -> dict:
    """Run the RAG agent directly (for notebook testing).

    Args:
        question: The user's question.
        thread_id: Optional conversation ID for multi-turn memory.

    Returns:
        dict with 'answer' and 'conversation_id' keys.
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
        if hasattr(item, "text"):
            answer = item.text
            break

    return {"answer": answer, "conversation_id": conv_id}


# ---------------------------------------------------------------------------
# MLflow model declaration (for "models from code" pattern)
# ---------------------------------------------------------------------------
AGENT = WikiRAGAgent()
mlflow.models.set_model(AGENT)
