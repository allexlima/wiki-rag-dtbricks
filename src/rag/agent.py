"""
LangGraph agentic RAG: retrieve → grade_documents → (rewrite_query loop) → generate.
"""
from __future__ import annotations

from typing import TypedDict

import psycopg2
from databricks_openai import DatabricksOpenAI
from langgraph.graph import END, StateGraph

from src.rag.retriever import RetrievedDoc, retrieve

LLM_MODEL = "databricks-meta-llama-3-3-70b-instruct"
MAX_REWRITES = 2


class AgentState(TypedDict):
    question: str
    documents: list[RetrievedDoc]
    generation: str
    rewrite_count: int


def build_agent(conn: psycopg2.extensions.connection) -> StateGraph:
    """Build and compile the LangGraph RAG agent."""

    client = DatabricksOpenAI()

    # --- Node: retrieve ---
    def retrieve_node(state: AgentState) -> dict:
        docs = retrieve(conn, state["question"], top_k=5)
        return {"documents": docs}

    # --- Node: grade_documents ---
    def grade_documents_node(state: AgentState) -> dict:
        question = state["question"]
        docs = state["documents"]

        if not docs:
            return {"documents": []}

        relevant: list[RetrievedDoc] = []
        for doc in docs:
            response = client.chat.completions.create(
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
                        "content": f"Question: {question}\n\nDocument: {doc.chunk_text}",
                    },
                ],
                max_tokens=3,
                temperature=0,
            )
            grade = response.choices[0].message.content.strip().lower()
            if grade.startswith("yes"):
                relevant.append(doc)

        return {"documents": relevant}

    # --- Node: rewrite_query ---
    def rewrite_query_node(state: AgentState) -> dict:
        response = client.chat.completions.create(
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
        new_question = response.choices[0].message.content.strip()
        return {
            "question": new_question,
            "rewrite_count": state.get("rewrite_count", 0) + 1,
        }

    # --- Node: generate ---
    def generate_node(state: AgentState) -> dict:
        docs = state["documents"]
        context = "\n\n".join(
            f"[{d.page_title}]\n{d.chunk_text}" for d in docs
        )
        response = client.chat.completions.create(
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
                    "content": f"Context:\n{context}\n\nQuestion: {state['question']}",
                },
            ],
            max_tokens=1024,
            temperature=0.1,
        )
        return {"generation": response.choices[0].message.content}

    # --- Conditional edge: should rewrite? ---
    def should_rewrite(state: AgentState) -> str:
        if state["documents"]:
            return "generate"
        if state.get("rewrite_count", 0) >= MAX_REWRITES:
            return "generate"
        return "rewrite_query"

    # --- Build graph ---
    graph = StateGraph(AgentState)

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


def run_agent(
    conn: psycopg2.extensions.connection,
    question: str,
) -> dict:
    """Run the RAG agent and return answer + sources."""
    agent = build_agent(conn)
    result = agent.invoke({
        "question": question,
        "documents": [],
        "generation": "",
        "rewrite_count": 0,
    })
    return {
        "answer": result["generation"],
        "sources": [
            {"title": d.page_title, "similarity": round(d.similarity, 4)}
            for d in result["documents"]
        ],
    }
