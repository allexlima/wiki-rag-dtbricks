"""
pgvector cosine similarity search against wiki_rag.wiki_embeddings.
"""
from __future__ import annotations

from dataclasses import dataclass

import psycopg2
from pgvector.psycopg2 import register_vector

from src.pipeline.embedder import embed_texts


@dataclass
class RetrievedDoc:
    chunk_id: int
    page_title: str
    chunk_text: str
    similarity: float


def retrieve(
    conn: psycopg2.extensions.connection,
    query: str,
    top_k: int = 5,
) -> list[RetrievedDoc]:
    """Embed query and search pgvector for the top_k most similar chunks."""
    register_vector(conn)

    # Embed the query
    query_embedding = embed_texts([query])[0]

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.chunk_id, c.page_title, c.chunk_text,
                   1 - (e.embedding <=> %s::vector) AS similarity
            FROM wiki_rag.wiki_embeddings e
            JOIN wiki_rag.wiki_chunks c ON c.chunk_id = e.chunk_id
            ORDER BY e.embedding <=> %s::vector
            LIMIT %s
            """,
            (query_embedding, query_embedding, top_k),
        )
        rows = cur.fetchall()

    return [
        RetrievedDoc(chunk_id=r[0], page_title=r[1], chunk_text=r[2], similarity=r[3])
        for r in rows
    ]
