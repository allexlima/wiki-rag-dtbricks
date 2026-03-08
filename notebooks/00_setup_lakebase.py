# Databricks notebook source

# COMMAND ----------

# MAGIC %md
# MAGIC # 00 — Lakebase Setup
# MAGIC 
# MAGIC One-time provisioning of the **Lakebase Provisioned PostgreSQL** backend
# MAGIC for the Wiki RAG pipeline.
# MAGIC 
# MAGIC | Step | Action |
# MAGIC |------|--------|
# MAGIC | 🏗️ | Provision Lakebase instance (PG 16, 1 CU) |
# MAGIC | 🗄️ | Create `wikidb` database |
# MAGIC | 🔐 | Enable native PG login & create `mediawiki` role |
# MAGIC | 🔑 | Store credentials in Databricks secret scope |
# MAGIC | 📐 | Enable pgvector · create `wiki_rag` schema, tables & indexes |
# MAGIC | ✅ | Verify setup with password auth |
# MAGIC 
# MAGIC > **Idempotent** — uses `IF NOT EXISTS` / `ON CONFLICT DO NOTHING` throughout.
# MAGIC >
# MAGIC > Lakebase is PostgreSQL — DDL runs via **psycopg2**, not `%sql`.

# COMMAND ----------

# MAGIC %pip install psycopg2-binary databricks-sdk pgvector --upgrade -q
# MAGIC %restart_python

# COMMAND ----------

# MAGIC %md
# MAGIC ## 🔧 Configuration

# COMMAND ----------

dbutils.widgets.text("instance_name", "wiki-rag-lakebase", "Lakebase Instance Name")
dbutils.widgets.text("db_name", "wikidb", "Database Name")
dbutils.widgets.text("secret_scope", "wiki-rag", "Secret Scope Name")

# COMMAND ----------

INSTANCE_NAME = dbutils.widgets.get("instance_name")
DB_NAME = dbutils.widgets.get("db_name")
DB_PORT = "5432"
MW_ROLE = "mediawiki"
SCOPE = dbutils.widgets.get("secret_scope")
SCHEMA = "wiki_rag"

# Password is read from the secret scope (stored by scripts/setup_secrets.py)
try:
    MW_PASSWORD = dbutils.secrets.get(SCOPE, "mw_password")
except Exception:
    raise ValueError(
        f"Secret 'mw_password' not found in scope '{SCOPE}'. "
        "Run 'make setup-secrets' or 'python scripts/setup_secrets.py' first."
    )

if not MW_PASSWORD or len(MW_PASSWORD) < 8:
    raise ValueError("Secret 'mw_password' must be at least 8 characters.")

# SCHEMA is interpolated into DDL f-strings below — must be a safe identifier
if not SCHEMA.isidentifier():
    raise ValueError(f"Invalid schema name: {SCHEMA}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 🏗️ Provision Lakebase instance

# COMMAND ----------

import uuid
from contextlib import closing

import psycopg2
from psycopg2 import sql

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound, ResourceAlreadyExists
from databricks.sdk.service.database import DatabaseInstance

w = WorkspaceClient()
CURRENT_USER = w.current_user.me().user_name


def _oauth_conn(dbname: str) -> psycopg2.extensions.connection:
    """Open an autocommit OAuth connection as the workspace owner (admin ops)."""
    cred = w.database.generate_database_credential(
        request_id=str(uuid.uuid4()),
        instance_names=[INSTANCE_NAME],
    )
    conn = psycopg2.connect(
        host=instance.read_write_dns,
        port=DB_PORT,
        dbname=dbname,
        user=CURRENT_USER,
        password=cred.token,
        sslmode="require",
    )
    conn.autocommit = True
    return conn


try:
    instance = w.database.get_database_instance(name=INSTANCE_NAME)
    print(f"✅ Instance '{INSTANCE_NAME}' exists (state={instance.state})")
except NotFound:
    print(f"⏳ Creating instance '{INSTANCE_NAME}' ...")
    instance = w.database.create_database_instance(
        database_instance=DatabaseInstance(
            name=INSTANCE_NAME,
            capacity="CU_1",
            pg_version="16",
        )
    ).result()
    print(f"✅ Instance '{INSTANCE_NAME}' created")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 🗄️ Create database
# MAGIC 
# MAGIC `CREATE DATABASE` requires autocommit — bootstrap via `databricks_postgres`.

# COMMAND ----------

with closing(_oauth_conn("databricks_postgres")) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB_NAME,))
        if cur.fetchone():
            print(f"✅ Database '{DB_NAME}' already exists")
        else:
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DB_NAME)))
            print(f"✅ Database '{DB_NAME}' created")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 🔐 Native PG login & MediaWiki role
# MAGIC 
# MAGIC OAuth tokens expire in ~1 h — unsuitable for long-running containers.
# MAGIC Native PG login gives the `mediawiki` role a static password.

# COMMAND ----------

if not instance.enable_pg_native_login:
    print("⏳ Enabling native PG login ...")
    op = w.database.update_database_instance(
        name=INSTANCE_NAME,
        database_instance=DatabaseInstance(
            name=INSTANCE_NAME,
            enable_pg_native_login=True,
        ),
        update_mask="enable_pg_native_login",
    )
    if hasattr(op, "result"):
        op.result()
    instance = w.database.get_database_instance(name=INSTANCE_NAME)
    print("✅ Native PG login enabled")
else:
    print("✅ Native PG login already enabled")

# COMMAND ----------

_role = sql.Identifier(MW_ROLE)

with closing(_oauth_conn(DB_NAME)) as conn:
    with conn.cursor() as cur:
        # Upsert role — CREATE or ALTER depending on existence
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (MW_ROLE,))
        verb = sql.SQL("ALTER ROLE") if cur.fetchone() else sql.SQL("CREATE ROLE")
        cur.execute(
            sql.SQL("{} {} WITH LOGIN PASSWORD %s").format(verb, _role),
            (MW_PASSWORD,),
        )
        print(f"✅ Role '{MW_ROLE}' upserted")

        # MediaWiki needs full ownership of the public schema
        grants = [
            sql.SQL("GRANT ALL ON DATABASE {} TO {}").format(
                sql.Identifier(DB_NAME),
                _role,
            ),
            sql.SQL("GRANT ALL ON SCHEMA {} TO {}").format(
                sql.Identifier("public"),
                _role,
            ),
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT ALL ON TABLES TO {}"
            ).format(sql.Identifier("public"), _role),
        ]
        for stmt in grants:
            cur.execute(stmt)
        print(f"✅ Grants applied to '{MW_ROLE}'")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 🔑 Store credentials in secret scope

# COMMAND ----------

try:
    w.secrets.create_scope(scope=SCOPE)
    print(f"✅ Scope '{SCOPE}' created")
except ResourceAlreadyExists:
    print(f"✅ Scope '{SCOPE}' already exists")

SECRETS = {
    "lakebase_instance_name": INSTANCE_NAME,
    "lakebase_user": CURRENT_USER,
    "lakebase_db": DB_NAME,
    "lakebase_host": instance.read_write_dns,
    "lakebase_port": DB_PORT,
    "mw_role": MW_ROLE,
    "mw_password": MW_PASSWORD,
}

print("✅ Secrets stored:\n")
for k, v in SECRETS.items():
    print(f"\t{k} = {v}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 📐 Schema, tables & indexes

# COMMAND ----------

# fmt: off
DDL: list[tuple[str, str]] = [
    ("pgvector extension",
     "CREATE EXTENSION IF NOT EXISTS vector;"),

    (f"{SCHEMA} schema",
     f"CREATE SCHEMA IF NOT EXISTS {SCHEMA};"),

    (f"{SCHEMA}.wiki_chunks",
     f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.wiki_chunks (
            chunk_id    BIGSERIAL   PRIMARY KEY,
            page_id     BIGINT      NOT NULL,
            page_title  TEXT        NOT NULL,
            page_ns     INT         NOT NULL DEFAULT 0,
            rev_id      BIGINT      NOT NULL,
            chunk_index INT         NOT NULL,
            chunk_text  TEXT        NOT NULL,
            created_at  TIMESTAMPTZ DEFAULT now()
        );"""),

    (f"{SCHEMA}.wiki_embeddings",
     f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.wiki_embeddings (
            embedding_id BIGSERIAL  PRIMARY KEY,
            chunk_id     BIGINT     REFERENCES {SCHEMA}.wiki_chunks(chunk_id) ON DELETE CASCADE,
            embedding    vector(1024) NOT NULL
        );"""),

    (f"{SCHEMA}.sync_state",
     f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.sync_state (
            key         TEXT        PRIMARY KEY,
            value       TEXT        NOT NULL,
            updated_at  TIMESTAMPTZ DEFAULT now()
        );"""),

    (f"idx: {SCHEMA}.wiki_chunks(page_id)",
     f"CREATE INDEX IF NOT EXISTS idx_chunks_page_id ON {SCHEMA}.wiki_chunks(page_id);"),

    # HNSW: ~95 % recall, sub-ms latency. m=16, ef_construction=64 balance build speed vs accuracy.
    (f"idx: HNSW on {SCHEMA}.wiki_embeddings(embedding)",
     f"""CREATE INDEX IF NOT EXISTS idx_embeddings_hnsw
            ON {SCHEMA}.wiki_embeddings
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64);"""),

    ("seed sync watermark",
     f"""INSERT INTO {SCHEMA}.sync_state (key, value)
        VALUES ('last_processed_rev_id', '0')
        ON CONFLICT (key) DO NOTHING;"""),

    # Image metadata (multimodal processing — vision LLM captions)
    (f"{SCHEMA}.wiki_images",
     f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.wiki_images (
            image_id    BIGSERIAL   PRIMARY KEY,
            page_id     BIGINT      NOT NULL,
            page_title  TEXT        NOT NULL,
            filename    TEXT        NOT NULL,
            alt_text    TEXT        DEFAULT '',
            caption     TEXT        NOT NULL,
            created_at  TIMESTAMPTZ DEFAULT now()
        );"""),

    (f"idx: {SCHEMA}.wiki_images(page_id)",
     f"CREATE INDEX IF NOT EXISTS idx_images_page_id ON {SCHEMA}.wiki_images(page_id);"),

    # Add chunk_source column to track text vs image-sourced chunks
    (f"alter: {SCHEMA}.wiki_chunks add chunk_source",
     f"ALTER TABLE {SCHEMA}.wiki_chunks ADD COLUMN IF NOT EXISTS chunk_source TEXT DEFAULT 'text';"),

    # Conversation memory tables (for multi-turn RAG with persistent history)
    (f"{SCHEMA}.conversations",
     f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.conversations (
            conversation_id UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id         TEXT        NOT NULL,
            created_at      TIMESTAMPTZ DEFAULT now(),
            updated_at      TIMESTAMPTZ DEFAULT now(),
            metadata        JSONB       DEFAULT '{{}}'::jsonb
        );"""),

    (f"{SCHEMA}.messages",
     f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.messages (
            message_id      BIGSERIAL   PRIMARY KEY,
            conversation_id UUID        NOT NULL REFERENCES {SCHEMA}.conversations(conversation_id) ON DELETE CASCADE,
            role            TEXT        NOT NULL,
            content         TEXT        NOT NULL,
            sources         JSONB,
            created_at      TIMESTAMPTZ DEFAULT now()
        );"""),

    (f"idx: {SCHEMA}.conversations(user_id, updated_at)",
     f"CREATE INDEX IF NOT EXISTS idx_conversations_user ON {SCHEMA}.conversations(user_id, updated_at DESC);"),

    (f"idx: {SCHEMA}.messages(conversation_id, created_at)",
     f"CREATE INDEX IF NOT EXISTS idx_messages_conv ON {SCHEMA}.messages(conversation_id, created_at);"),

    # Grant mediawiki role access to wiki_rag schema (needed for serving + ingestion)
    (f"grant {SCHEMA} to {MW_ROLE}",
     f"GRANT USAGE ON SCHEMA {SCHEMA} TO {MW_ROLE};"),

    (f"grant tables in {SCHEMA} to {MW_ROLE}",
     f"GRANT ALL ON ALL TABLES IN SCHEMA {SCHEMA} TO {MW_ROLE};"),

    (f"grant sequences in {SCHEMA} to {MW_ROLE}",
     f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {SCHEMA} TO {MW_ROLE};"),

    (f"default privileges on {SCHEMA} for {MW_ROLE}",
     f"ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA} GRANT ALL ON TABLES TO {MW_ROLE};"),

    (f"default privileges on sequences in {SCHEMA} for {MW_ROLE}",
     f"ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA} GRANT USAGE, SELECT ON SEQUENCES TO {MW_ROLE};"),
]
# fmt: on

# DDL needs owner privileges (e.g. CREATE EXTENSION) — use OAuth admin connection
with closing(_oauth_conn(DB_NAME)) as conn:
    with conn.cursor() as cur:
        print("📐 Running DDL:")
        for label, stmt in DDL:
            cur.execute(stmt)
            print(f"  ✅ {label}")

print("\n✅ DDL complete")

# COMMAND ----------

# MAGIC %md
# MAGIC ## ✅ Verify setup

# COMMAND ----------

import os
import sys

# Add repo root to sys.path so `from src.* import ...` works.
# Databricks Repos sets CWD to repo root; notebooks/ subfolder needs parent.
for _candidate in [os.getcwd(), os.path.join(os.getcwd(), "..")]:
    if os.path.isdir(os.path.join(_candidate, "src")):
        sys.path.insert(0, os.path.abspath(_candidate))
        break

from src.config import get_lakebase_conn

# Verify the password-auth path works end-to-end
with closing(get_lakebase_conn(w)) as conn:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s ORDER BY table_name",
            (SCHEMA,),
        )
        tables = [r[0] for r in cur.fetchall()]

        cur.execute(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = %s ORDER BY indexname",
            (SCHEMA,),
        )
        indexes = cur.fetchall()

print(f"📋 Tables in {SCHEMA}:")
for t in tables:
    print(f"  • {t}")

print(f"\n📋 Indexes in {SCHEMA}:")
for name, defn in indexes:
    print(f"  • {name}")
    print(f"    {defn}")

print("\n🎉 Done — Lakebase is ready")
