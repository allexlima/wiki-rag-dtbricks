"""
Generates embeddings using Databricks Foundation Model API.
"""
from __future__ import annotations

from databricks_openai import DatabricksOpenAI

EMBEDDING_MODEL = "databricks-gte-large-en"
EMBEDDING_DIMS = 1024
BATCH_SIZE = 64


def embed_texts(texts: list[str], model: str = EMBEDDING_MODEL) -> list[list[float]]:
    """
    Embed a list of texts via Foundation Model API.
    Returns list of 1024-dim vectors, one per input text.
    """
    client = DatabricksOpenAI()
    all_embeddings: list[list[float]] = []

    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i : i + BATCH_SIZE]
        response = client.embeddings.create(model=model, input=batch)
        all_embeddings.extend([item.embedding for item in response.data])

    return all_embeddings
