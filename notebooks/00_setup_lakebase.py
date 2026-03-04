# Databricks notebook source

# COMMAND ----------

# MAGIC %md
# MAGIC # 00 — Lakebase Setup
# MAGIC 
# MAGIC One-time provisioning of the Lakebase Provisioned PostgreSQL backend for the Wiki RAG pipeline.
# MAGIC 
# MAGIC **Steps:**
# MAGIC 1. Provisions a Lakebase instance (PG 17, 1 CU)
# MAGIC 2. Creates the `wikidb` database
# MAGIC 3. Enables native PG login and creates a `mediawiki` role (static password for MediaWiki)
# MAGIC 4. Stores connection credentials in a Databricks secret scope
# MAGIC 5. Enables `pgvector` and creates the `wiki_rag` schema, tables, and indexes
# MAGIC 
# MAGIC > Idempotent — all operations use `IF NOT EXISTS` / `ON CONFLICT DO NOTHING`.
# MAGIC >
# MAGIC > Lakebase is PostgreSQL, so DDL runs via **psycopg2** — not `%sql` (Spark SQL).

# COMMAND ----------

# MAGIC %pip install psycopg2-binary databricks-sdk pgvector --upgrade -q
# MAGIC %restart_python

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration

# COMMAND ----------

dbutils.widgets.text("instance_name", "wiki-rag-lakebase", "Lakebase Instance Name")
dbutils.widgets.text("mw_password", "", "MediaWiki PG Role Password")

INSTANCE_NAME = dbutils.widgets.get("instance_name")
MW_PASSWORD = dbutils.widgets.get("mw_password")
DB_NAME = "wikidb"
MW_ROLE = "mediawiki"
SCOPE = "wiki-rag"
SCHEMA = "wiki_rag"

if not MW_PASSWORD:
    raise ValueError(
        "Set the 'mw_password' widget — this will be the static password "
        "for the 'mediawiki' PG role used by the MediaWiki container."
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Provision Lakebase instance

# COMMAND ----------

import uuid

import psycopg2
from psycopg2 import sql

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound, ResourceAlreadyExists
from databricks.sdk.service.database import DatabaseInstance

w = WorkspaceClient()
current_user = w.current_user.me().user_name

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
# MAGIC ## Create database
# MAGIC 
# MAGIC `CREATE DATABASE` requires autocommit, so we bootstrap via the default
# MAGIC `databricks_postgres` database first.

# COMMAND ----------

cred = w.database.generate_database_credential(
    request_id=str(uuid.uuid4()),
    instance_names=[INSTANCE_NAME],
)

bootstrap_conn = psycopg2.connect(
    host=instance.read_write_dns,
    dbname="databricks_postgres",
    user=current_user,
    password=cred.token,
    sslmode="require",
)
bootstrap_conn.autocommit = True

with bootstrap_conn.cursor() as cur:
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB_NAME,))
    if cur.fetchone():
        print(f"✓ Database '{DB_NAME}' already exists")
    else:
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DB_NAME)))
        print(f"✓ Database '{DB_NAME}' created")

bootstrap_conn.close()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Enable native PG login and create MediaWiki role
# MAGIC
# MAGIC OAuth tokens expire after ~1 hour — unsuitable for a long-running MediaWiki container.
# MAGIC Native PG login lets us create a standard PostgreSQL role with a static password.

# COMMAND ----------

# Enable native PG login on the instance (idempotent — no-op if already enabled)
if not instance.enable_pg_native_login:
    print("⏳ Enabling native PG login ...")
    instance = w.database.update_database_instance(
        name=INSTANCE_NAME,
        database_instance=DatabaseInstance(enable_pg_native_login=True),
        update_mask="enable_pg_native_login",
    )
    print("✓ Native PG login enabled")
else:
    print("✓ Native PG login already enabled")

# COMMAND ----------

# Create a dedicated role for MediaWiki with a static password.
# Needs a fresh OAuth connection to wikidb for the GRANT statements.
cred = w.database.generate_database_credential(
    request_id=str(uuid.uuid4()),
    instance_names=[INSTANCE_NAME],
)
role_conn = psycopg2.connect(
    host=instance.read_write_dns,
    dbname=DB_NAME,
    user=current_user,
    password=cred.token,
    sslmode="require",
)
role_conn.autocommit = True

with role_conn.cursor() as cur:
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (MW_ROLE,))
    if cur.fetchone():
        # Update password in case it changed
        cur.execute(
            sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD %s").format(sql.Identifier(MW_ROLE)),
            (MW_PASSWORD,),
        )
        print(f"✓ Role '{MW_ROLE}' already exists — password updated")
    else:
        cur.execute(
            sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD %s").format(sql.Identifier(MW_ROLE)),
            (MW_PASSWORD,),
        )
        print(f"✓ Role '{MW_ROLE}' created")

    # Grant permissions: MediaWiki needs full ownership of its schema
    cur.execute(sql.SQL("GRANT ALL ON DATABASE {} TO {}").format(
        sql.Identifier(DB_NAME), sql.Identifier(MW_ROLE),
    ))
    cur.execute(sql.SQL("GRANT ALL ON SCHEMA {} TO {}").format(
        sql.Identifier("public"), sql.Identifier(MW_ROLE),
    ))
    print(f"✓ Grants applied to '{MW_ROLE}' on '{DB_NAME}'")

role_conn.close()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Store credentials in secret scope

# COMMAND ----------

try:
    w.secrets.create_scope(scope=SCOPE)
    print(f"✓ Secret scope '{SCOPE}' created")
except ResourceAlreadyExists:
    print(f"✓ Secret scope '{SCOPE}' already exists")

secrets = {
    "lakebase_instance_name": INSTANCE_NAME,
    "lakebase_user": current_user,
    "lakebase_db": DB_NAME,
    "lakebase_host": instance.read_write_dns,
    "mw_role": MW_ROLE,
    "mw_password": MW_PASSWORD,
}
for key, value in secrets.items():
    w.secrets.put_secret(scope=SCOPE, key=key, string_value=value)

SAFE_TO_DISPLAY = {"lakebase_instance_name", "lakebase_user", "lakebase_db", "mw_role"}

print("✓ Secrets stored:")
for key in secrets:
    display = secrets[key] if key in SAFE_TO_DISPLAY else "********"
    print(f"  {key} = {display}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Connect to `wikidb` and run DDL

# COMMAND ----------

import os
import sys

sys.path.insert(0, os.path.join(os.getcwd(), ".."))

from src.config import get_lakebase_conn

conn = get_lakebase_conn(w)


def run_ddl(
    connection: psycopg2.extensions.connection, label: str, statement: str
) -> None:
    """Execute a DDL statement and commit."""
    with connection.cursor() as cur:
        cur.execute(statement)
    connection.commit()
    print(f"  ✓ {label}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Create schema, tables, and indexes

# COMMAND ----------

# fmt: off
DDL_STEPS: list[tuple[str, str]] = [
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

    ("B-tree index on wiki_chunks(page_id)",
     f"CREATE INDEX IF NOT EXISTS idx_chunks_page_id ON {SCHEMA}.wiki_chunks(page_id);"),

    # HNSW: ~95% recall with sub-ms latency. m=16, ef_construction=64
    # balance build speed vs query accuracy.
    ("HNSW index on wiki_embeddings(embedding)",
     f"""CREATE INDEX IF NOT EXISTS idx_embeddings_hnsw
            ON {SCHEMA}.wiki_embeddings
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64);"""),

    ("Seed sync watermark",
     f"""INSERT INTO {SCHEMA}.sync_state (key, value)
        VALUES ('last_processed_rev_id', '0')
        ON CONFLICT (key) DO NOTHING;"""),
]
# fmt: on

print("Running DDL:")
for label, statement in DDL_STEPS:
    run_ddl(conn, label, statement)
print("\n✓ DDL complete")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Verify setup

# COMMAND ----------

with conn.cursor() as cur:
    cur.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = %s ORDER BY table_name;",
        (SCHEMA,),
    )
    tables = [row[0] for row in cur.fetchall()]

    cur.execute(
        "SELECT indexname, indexdef FROM pg_indexes "
        "WHERE schemaname = %s ORDER BY indexname;",
        (SCHEMA,),
    )
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
