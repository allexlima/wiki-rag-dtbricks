# Databricks notebook source

# COMMAND ----------

# MAGIC %md
# MAGIC # 00 — Lakebase Setup
# MAGIC 
# MAGIC One-time provisioning of the Lakebase Provisioned PostgreSQL instance for the Wiki RAG pipeline.
# MAGIC 
# MAGIC **What this notebook does:**
# MAGIC 1. Creates a Lakebase Provisioned instance (PG 17)
# MAGIC 2. Stores connection credentials in a Databricks secret scope
# MAGIC 3. Enables the `pgvector` extension for embedding storage
# MAGIC 4. Creates the `wiki_rag` schema with tables for chunks, embeddings, and sync state
# MAGIC 5. Creates indexes (B-tree + HNSW) for efficient retrieval
# MAGIC 
# MAGIC > Idempotent — all DDL uses `IF NOT EXISTS` / `ON CONFLICT DO NOTHING`, safe to re-run.
# MAGIC >
# MAGIC > Lakebase is PostgreSQL, so DDL runs via **psycopg2** — not `%sql` (which targets Spark SQL).

# COMMAND ----------

# MAGIC %pip install psycopg2-binary databricks-sdk pgvector --upgrade -q
# MAGIC %restart_python

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration

# COMMAND ----------

dbutils.widgets.text("instance_name", "wiki-rag-lakebase", "Lakebase Instance Name")
dbutils.widgets.text("db_name", "databricks_postgres", "Database Name")

INSTANCE_NAME = dbutils.widgets.get("instance_name")
DB_NAME = dbutils.widgets.get("db_name")
SCOPE = "wiki-rag"
SCHEMA = "wiki_rag"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Provision Lakebase instance

# COMMAND ----------

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound, ResourceAlreadyExists
from databricks.sdk.service.database import DatabaseInstance

w = WorkspaceClient()

try:
    instance = w.database.get_database_instance(name=INSTANCE_NAME)
    print(f"✓ Instance '{INSTANCE_NAME}' already exists (state={instance.state})")
except NotFound:
    print(f"⏳ Creating Lakebase instance '{INSTANCE_NAME}' ...")
    instance = w.database.create_database_instance(
        database_instance=DatabaseInstance(
            name=INSTANCE_NAME,
            capacity="CU_1",
            pg_version="17",
        )
    ).result()
    print(f"✓ Instance '{INSTANCE_NAME}' created")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Store credentials in secret scope

# COMMAND ----------

try:
    w.secrets.create_scope(scope=SCOPE)
    print(f"✓ Secret scope '{SCOPE}' created")
except ResourceAlreadyExists:
    print(f"✓ Secret scope '{SCOPE}' already exists")

instance = w.database.get_database_instance(name=INSTANCE_NAME)
endpoint_host = instance.read_write_dns
current_user = w.current_user.me().user_name

secrets = {
    "lakebase_instance_name": INSTANCE_NAME,
    "lakebase_user": current_user,
    "lakebase_db": DB_NAME,
    "lakebase_host": endpoint_host,
}
for key, value in secrets.items():
    w.secrets.put_secret(scope=SCOPE, key=key, string_value=value)

print("✓ Secrets stored:")
for key, value in secrets.items():
    print(f"  {key} = {value}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Connect and run DDL
# MAGIC 
# MAGIC Opens a psycopg2 connection via OAuth, then runs all schema setup in sequence.

# COMMAND ----------

import os
import sys

sys.path.insert(0, os.path.join(os.getcwd(), ".."))

from src.config import get_lakebase_conn

conn = get_lakebase_conn(w)


def run_ddl(label: str, sql: str) -> None:
    """Execute a single DDL statement and print the result."""
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    print(f"  ✓ {label}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Create schema, tables, and indexes

# COMMAND ----------

DDL_STEPS: list[tuple[str, str]] = [
    (
        "pgvector extension",
        "CREATE EXTENSION IF NOT EXISTS vector;",
    ),
    (
        f"{SCHEMA} schema",
        f"CREATE SCHEMA IF NOT EXISTS {SCHEMA};",
    ),
    (
        f"{SCHEMA}.wiki_chunks",
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA}.wiki_chunks (
            chunk_id    BIGSERIAL   PRIMARY KEY,
            page_id     BIGINT      NOT NULL,
            page_title  TEXT        NOT NULL,
            page_ns     INT         NOT NULL DEFAULT 0,
            rev_id      BIGINT      NOT NULL,
            chunk_index INT         NOT NULL,
            chunk_text  TEXT        NOT NULL,
            created_at  TIMESTAMPTZ DEFAULT now()
        );
        """,
    ),
    (
        f"{SCHEMA}.wiki_embeddings",
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA}.wiki_embeddings (
            embedding_id BIGSERIAL  PRIMARY KEY,
            chunk_id     BIGINT     REFERENCES {SCHEMA}.wiki_chunks(chunk_id) ON DELETE CASCADE,
            embedding    vector(1024) NOT NULL
        );
        """,
    ),
    (
        f"{SCHEMA}.sync_state",
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA}.sync_state (
            key         TEXT        PRIMARY KEY,
            value       TEXT        NOT NULL,
            updated_at  TIMESTAMPTZ DEFAULT now()
        );
        """,
    ),
    (
        "B-tree index on wiki_chunks(page_id)",
        f"CREATE INDEX IF NOT EXISTS idx_chunks_page_id ON {SCHEMA}.wiki_chunks(page_id);",
    ),
    (
        # HNSW gives ~95% recall with sub-ms latency for cosine similarity.
        # m=16, ef_construction=64 balance build speed vs. query accuracy.
        "HNSW index on wiki_embeddings(embedding)",
        f"""
        CREATE INDEX IF NOT EXISTS idx_embeddings_hnsw
            ON {SCHEMA}.wiki_embeddings
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64);
        """,
    ),
    (
        "Seed sync watermark",
        f"""
        INSERT INTO {SCHEMA}.sync_state (key, value)
        VALUES ('last_processed_rev_id', '0')
        ON CONFLICT (key) DO NOTHING;
        """,
    ),
]

print("Running DDL:")
for label, sql in DDL_STEPS:
    run_ddl(label, sql)
print("\n✓ DDL complete")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Verify setup

# COMMAND ----------

with conn.cursor() as cur:
    cur.execute(f"""
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = '{SCHEMA}'
        ORDER BY table_name;
    """)
    tables = [row[0] for row in cur.fetchall()]

    cur.execute(f"""
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = '{SCHEMA}'
        ORDER BY indexname;
    """)
    indexes = cur.fetchall()

print(f"Tables in {SCHEMA}:")
for t in tables:
    print(f"  • {t}")

print("\nIndexes:")
for idx_name, idx_def in indexes:
    print(f"  • {idx_name}")
    print(f"    {idx_def}")

# COMMAND ----------

conn.close()
print("✓ Done — Lakebase is ready")

# COMMAND ----------


