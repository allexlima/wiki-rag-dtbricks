# Databricks notebook source
# MAGIC %md
# MAGIC # 00 — Lakebase Setup
# MAGIC
# MAGIC Creates the `wiki_rag` schema, tables, pgvector extension, and HNSW index.
# MAGIC Idempotent — safe to run multiple times.

# COMMAND ----------

# MAGIC %pip install psycopg2-binary databricks-sdk pgvector --upgrade -q
# MAGIC %restart_python

# COMMAND ----------

import uuid
import psycopg2
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()
INSTANCE = dbutils.secrets.get("wiki-rag", "lakebase_instance_name")
DB_USER = dbutils.secrets.get("wiki-rag", "lakebase_user")
DB_NAME = dbutils.secrets.get("wiki-rag", "lakebase_db")

def get_conn():
    instance = w.database.get_database_instance(name=INSTANCE)
    cred = w.database.generate_database_credential(
        request_id=str(uuid.uuid4()),
        instance_names=[INSTANCE],
    )
    return psycopg2.connect(
        host=instance.read_write_dns,
        dbname=DB_NAME,
        user=DB_USER,
        password=cred.token,
        sslmode="require",
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Create schema, tables, and indexes

# COMMAND ----------

DDL = """
-- Enable pgvector
CREATE EXTENSION IF NOT EXISTS vector;

-- RAG schema (separate from mediawiki schema)
CREATE SCHEMA IF NOT EXISTS wiki_rag;

-- Processed text chunks
CREATE TABLE IF NOT EXISTS wiki_rag.wiki_chunks (
    chunk_id    BIGSERIAL   PRIMARY KEY,
    page_id     BIGINT      NOT NULL,
    page_title  TEXT        NOT NULL,
    page_ns     INT         NOT NULL DEFAULT 0,
    rev_id      BIGINT      NOT NULL,
    chunk_index INT         NOT NULL,
    chunk_text  TEXT        NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- Embeddings (1024-dim for databricks-gte-large-en)
CREATE TABLE IF NOT EXISTS wiki_rag.wiki_embeddings (
    embedding_id BIGSERIAL  PRIMARY KEY,
    chunk_id     BIGINT     REFERENCES wiki_rag.wiki_chunks(chunk_id) ON DELETE CASCADE,
    embedding    vector(1024) NOT NULL
);

-- Sync watermark for incremental processing
CREATE TABLE IF NOT EXISTS wiki_rag.sync_state (
    key         TEXT        PRIMARY KEY,
    value       TEXT        NOT NULL,
    updated_at  TIMESTAMPTZ DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_chunks_page_id
    ON wiki_rag.wiki_chunks(page_id);

CREATE INDEX IF NOT EXISTS idx_embeddings_hnsw
    ON wiki_rag.wiki_embeddings
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Seed watermark
INSERT INTO wiki_rag.sync_state (key, value)
VALUES ('last_processed_rev_id', '0')
ON CONFLICT (key) DO NOTHING;
"""

with get_conn() as conn:
    with conn.cursor() as cur:
        cur.execute(DDL)
    conn.commit()

print("Lakebase setup complete: wiki_rag schema, tables, and indexes created.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Verify tables exist

# COMMAND ----------

with get_conn() as conn:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'wiki_rag'
            ORDER BY table_name;
        """)
        tables = [row[0] for row in cur.fetchall()]

print(f"Tables in wiki_rag schema: {tables}")
