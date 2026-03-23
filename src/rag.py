"""Wiki RAG Agent — ResponsesAgent with LangGraph agentic RAG flow."""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Generator, TypedDict

import mlflow
import psycopg2
from databricks_langchain import ChatDatabricks
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

LLM_MODEL = os.environ.get("LLM_MODEL", "databricks-claude-sonnet-4-6")
MAX_REWRITES = 2
LLM_TIMEOUT = 60
MAX_HISTORY_TURNS = 5


@dataclass
class RetrievedDoc:

    chunk_id: int
    page_title: str
    chunk_text: str
    similarity: float
    chunk_source: str = "text"


class _RAGState(TypedDict):
    question: str
    documents: list[dict]
    generation: str
    rewrite_count: int
    conversation_history: str


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=30))
def _llm_call(llm: ChatDatabricks, messages: list[dict], **kwargs) -> str:
    """Make an LLM call with retry, returning stripped content."""
    from langchain_core.messages import HumanMessage, SystemMessage

    lc_messages = []
    for m in messages:
        if m["role"] == "system":
            lc_messages.append(SystemMessage(content=m["content"]))
        else:
            lc_messages.append(HumanMessage(content=m["content"]))
    response = llm.invoke(lc_messages, **kwargs)
    return (response.content or "").strip()


class WikiRAGAgent(ResponsesAgent):
    """Agentic RAG over a MediaWiki knowledge base (MLflow ResponsesAgent)."""

    def __init__(self) -> None:
        """Initialise with lazy clients (created on first use)."""
        self._llm: ChatDatabricks | None = None
        self._conn = None
        self._graph = None

    @property
    def llm(self) -> ChatDatabricks:
        """Lazy-initialised ChatDatabricks LLM client."""
        if self._llm is None:
            self._llm = ChatDatabricks(endpoint=LLM_MODEL)
        return self._llm

    def _get_conn(self) -> psycopg2.extensions.connection:
        """Return a live psycopg2 connection, reconnecting if needed."""
        if self._conn is None or self._conn.closed:
            from src.config import get_lakebase_conn

            self._conn = get_lakebase_conn()
        return self._conn

    def retrieve(
        self,
        conn: psycopg2.extensions.connection,
        query: str,
        top_k: int = 5,
    ) -> list[RetrievedDoc]:
        """Embed query and search pgvector for top_k most similar chunks."""
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

    def _load_history(self, conversation_id: str) -> str:
        """Load recent conversation history from Lakebase."""
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
        """Persist user question and assistant answer to Lakebase."""
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

    def _build_graph(self):
        """Build and compile the LangGraph RAG state graph."""
        llm = self.llm

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
                    llm,
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
                llm,
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
                llm,
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

    def predict(self, request: ResponsesAgentRequest) -> ResponsesAgentResponse:
        """Run the full RAG pipeline and return a ResponsesAgentResponse."""
        outputs = [
            event.item
            for event in self.predict_stream(request)
            if event.type == "response.output_item.done"
        ]
        return ResponsesAgentResponse(output=outputs)

    def predict_stream(
        self, request: ResponsesAgentRequest,
    ) -> Generator[ResponsesAgentStreamEvent, None, None]:
        """Stream the RAG pipeline, yielding events as each node completes."""
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

        if self._graph is None:
            self._graph = self._build_graph()
        graph = self._graph
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


def run_agent(question: str, thread_id: str | None = None) -> dict:
    """Run the RAG agent directly (for notebook testing)."""
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


AGENT = WikiRAGAgent()
mlflow.models.set_model(AGENT)
