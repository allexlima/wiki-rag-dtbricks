"""
Generates embeddings using Databricks Foundation Model API.
"""
from __future__ import annotations

import logging

from databricks_openai import DatabricksOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

EMBEDDING_MODEL = "databricks-gte-large-en"
EMBEDDING_DIMS = 1024
BATCH_SIZE = 64


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=30))
def _embed_batch(client: DatabricksOpenAI, model: str, batch: list[str]) -> list[list[float]]:
    """Embed a single batch with retry on transient failures."""
    response = client.embeddings.create(model=model, input=batch)
    return [item.embedding for item in response.data]


def embed_texts(texts: list[str], model: str = EMBEDDING_MODEL) -> list[list[float]]:
    """
    Embed a list of texts via Foundation Model API.
    Returns list of 1024-dim vectors, one per input text.
    """
    client = DatabricksOpenAI()
    all_embeddings: list[list[float]] = []

    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i : i + BATCH_SIZE]
        log.debug("Embedding batch %d–%d of %d texts", i, i + len(batch), len(texts))
        embeddings = _embed_batch(client, model, batch)
        all_embeddings.extend(embeddings)

    return all_embeddings
