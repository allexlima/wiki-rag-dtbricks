# Databricks notebook source

# COMMAND ----------

# MAGIC %md
# MAGIC # 00 — Lakebase Setup
# MAGIC 
# MAGIC Provisions a **Lakebase Autoscaling PG 16** instance hosting both MediaWiki
# MAGIC native tables (`public` schema) and RAG pipeline tables (`wiki_rag` schema).
# MAGIC 
# MAGIC **Prerequisites:** Run `make setup-secrets` first.
# MAGIC 
# MAGIC > **Idempotent** — safe to re-run. All DDL uses `IF NOT EXISTS` / `ON CONFLICT DO NOTHING`.

# COMMAND ----------

# MAGIC %pip install psycopg2-binary "databricks-sdk>=0.81" pgvector --upgrade -q
# MAGIC %restart_python

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration
# MAGIC 
# MAGIC Auto-populated by the DAB job, or set manually via widgets.

# COMMAND ----------

dbutils.widgets.text("instance_name", "wiki-rag-lakebase", "Lakebase Project")
dbutils.widgets.text("db_name", "wikidb", "Database Name")
dbutils.widgets.text("secret_scope", "wiki-rag", "Secret Scope")

# COMMAND ----------

from contextlib import closing

import psycopg2
from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound, ResourceAlreadyExists, ResourceDoesNotExist
from databricks.sdk.service.postgres import (
    Duration,
    Project,
    ProjectDefaultEndpointSettings,
    ProjectSpec,
)
from psycopg2 import sql

# ─── Parameters ───────────────────────────────────────────────────────

PROJECT_ID = dbutils.widgets.get("instance_name")  # reuse widget name for DAB compat
DB_NAME = dbutils.widgets.get("db_name")
SCOPE = dbutils.widgets.get("secret_scope")
DB_PORT = "5432"
MW_ROLE = "mediawiki"
SCHEMA = "wiki_rag"

# Autoscaling resource paths
PROJECT_PATH = f"projects/{PROJECT_ID}"
BRANCH_PATH = f"{PROJECT_PATH}/branches/production"
ENDPOINT_PATH = f"{BRANCH_PATH}/endpoints/primary"

# ─── Validate prerequisites ──────────────────────────────────────────

try:
    MW_PASSWORD = dbutils.secrets.get(SCOPE, "mw_password")
    assert MW_PASSWORD and len(MW_PASSWORD) >= 8, "Password must be >= 8 chars."
except ResourceDoesNotExist:
    raise SystemExit(
        f"\n❌ Secret scope '{SCOPE}' does not exist.\n"
        f"   Run 'make setup-secrets' first to create it and store the password.\n"
    )
except Exception as e:
    if "does not exist" in str(e).lower():
        raise SystemExit(
            f"\n❌ Secret 'mw_password' not found in scope '{SCOPE}'.\n"
            f"   Run 'make setup-secrets' first.\n"
        )
    raise
assert SCHEMA.isidentifier(), f"Invalid schema name: {SCHEMA}"

w = WorkspaceClient()
CURRENT_USER = w.current_user.me().user_name
print(f"🔧 User: {CURRENT_USER}  Project: {PROJECT_ID}  DB: {DB_NAME}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Create Lakebase Autoscaling project
# MAGIC 
# MAGIC Creates the PG 16 project with auto-scaling + scale-to-zero. `.wait()` blocks until ready (~2-3 min).

# COMMAND ----------

# Autoscaling + scale-to-zero settings (applied at project creation)
AUTOSCALE_MIN_CU = 0.5   # minimum compute (cost-optimized)
AUTOSCALE_MAX_CU = 2.0   # maximum compute (handles load spikes)
SCALE_TO_ZERO_SECONDS = 300  # 5 min idle → suspend compute

try:
    project = w.postgres.get_project(name=PROJECT_PATH)
    print(f"✅ Project '{PROJECT_ID}' exists (pg_version={project.status.pg_version})")
except NotFound:
    print(f"⏳ Creating project '{PROJECT_ID}' (this may take a few minutes)...")
    project = w.postgres.create_project(
        project=Project(
            spec=ProjectSpec(
                display_name=PROJECT_ID,
                pg_version="16",
                default_endpoint_settings=ProjectDefaultEndpointSettings(
                    autoscaling_limit_min_cu=AUTOSCALE_MIN_CU,
                    autoscaling_limit_max_cu=AUTOSCALE_MAX_CU,
                    suspend_timeout_duration=Duration(seconds=SCALE_TO_ZERO_SECONDS),
                ),
            )
        ),
        project_id=PROJECT_ID,
    ).wait()
    print(f"✅ Project created ({AUTOSCALE_MIN_CU}-{AUTOSCALE_MAX_CU} CU, scale-to-zero={SCALE_TO_ZERO_SECONDS}s)")

# Get the primary endpoint DNS
endpoint = w.postgres.get_endpoint(name=ENDPOINT_PATH)
HOST = endpoint.status.hosts.host
print(f"✅ Endpoint {endpoint.status.current_state} → {HOST}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Create database
# MAGIC 
# MAGIC Creates the `wikidb` database using a short-lived OAuth token.

# COMMAND ----------

def _oauth_conn(dbname: str) -> psycopg2.extensions.connection:
    """Open an autocommit OAuth connection as the workspace owner (admin ops)."""
    cred = w.postgres.generate_database_credential(endpoint=ENDPOINT_PATH)
    conn = psycopg2.connect(
        host=HOST,
        port=DB_PORT,
        dbname=dbname,
        user=CURRENT_USER,
        password=cred.token,
        sslmode="require",
        connect_timeout=30,
    )
    conn.autocommit = True
    return conn


# CREATE DATABASE requires autocommit — connect to the default `databricks_postgres` first
with closing(_oauth_conn("databricks_postgres")) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB_NAME,))
        if cur.fetchone():
            print(f"✅ Database '{DB_NAME}' exists")
        else:
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DB_NAME)))
            print(f"✅ Database '{DB_NAME}' created")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Create role + grants
# MAGIC 
# MAGIC Creates the `mediawiki` role (used by MW container + serving endpoint) with full access to `public` schema.

# COMMAND ----------

_role = sql.Identifier(MW_ROLE)

with closing(_oauth_conn(DB_NAME)) as conn:
    with conn.cursor() as cur:
        # Upsert role
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (MW_ROLE,))
        verb = sql.SQL("ALTER ROLE") if cur.fetchone() else sql.SQL("CREATE ROLE")
        cur.execute(
            sql.SQL("{} {} WITH LOGIN PASSWORD %s").format(verb, _role), (MW_PASSWORD,)
        )
        print(f"✅ Role '{MW_ROLE}' ready")

        # MediaWiki needs full ownership of public schema
        for stmt in [
            sql.SQL("GRANT ALL ON DATABASE {} TO {}").format(
                sql.Identifier(DB_NAME), _role
            ),
            sql.SQL("GRANT ALL ON SCHEMA public TO {}").format(_role),
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO {}"
            ).format(_role),
        ]:
            cur.execute(stmt)
        print("✅ Public schema grants applied")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Schema, tables & pgvector index
# MAGIC 
# MAGIC Creates `wiki_rag` schema: chunks, embeddings (HNSW), images, sync_state, conversations, messages. Grants `mediawiki` role access.

# COMMAND ----------

# fmt: off
DDL = [
    # Extensions & schema
    ("pgvector",               "CREATE EXTENSION IF NOT EXISTS vector;"),
    (f"{SCHEMA} schema",       f"CREATE SCHEMA IF NOT EXISTS {SCHEMA};"),

    # RAG tables
    (f"{SCHEMA}.wiki_chunks",  f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA}.wiki_chunks (
            chunk_id     BIGSERIAL   PRIMARY KEY,
            page_id      BIGINT      NOT NULL,
            page_title   TEXT        NOT NULL,
            page_ns      INT         NOT NULL DEFAULT 0,
            rev_id       BIGINT      NOT NULL,
            chunk_index  INT         NOT NULL,
            chunk_text   TEXT        NOT NULL,
            chunk_source TEXT        DEFAULT 'text',
            created_at   TIMESTAMPTZ DEFAULT now()
        );"""),

    (f"{SCHEMA}.wiki_embeddings", f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA}.wiki_embeddings (
            embedding_id BIGSERIAL   PRIMARY KEY,
            chunk_id     BIGINT      REFERENCES {SCHEMA}.wiki_chunks(chunk_id) ON DELETE CASCADE,
            embedding    vector(1024) NOT NULL
        );"""),

    (f"{SCHEMA}.wiki_images",  f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA}.wiki_images (
            image_id     BIGSERIAL   PRIMARY KEY,
            page_id      BIGINT      NOT NULL,
            page_title   TEXT        NOT NULL,
            filename     TEXT        NOT NULL,
            alt_text     TEXT        DEFAULT '',
            caption      TEXT        NOT NULL,
            created_at   TIMESTAMPTZ DEFAULT now()
        );"""),

    (f"{SCHEMA}.sync_state",   f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA}.sync_state (
            key          TEXT        PRIMARY KEY,
            value        TEXT        NOT NULL,
            updated_at   TIMESTAMPTZ DEFAULT now()
        );"""),

    # Conversation memory
    (f"{SCHEMA}.conversations", f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA}.conversations (
            conversation_id UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id         TEXT        NOT NULL,
            created_at      TIMESTAMPTZ DEFAULT now(),
            updated_at      TIMESTAMPTZ DEFAULT now(),
            metadata        JSONB       DEFAULT '{{}}'::jsonb
        );"""),

    (f"{SCHEMA}.messages",     f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA}.messages (
            message_id      BIGSERIAL   PRIMARY KEY,
            conversation_id UUID        NOT NULL REFERENCES {SCHEMA}.conversations(conversation_id) ON DELETE CASCADE,
            role            TEXT        NOT NULL,
            content         TEXT        NOT NULL,
            sources         JSONB,
            created_at      TIMESTAMPTZ DEFAULT now()
        );"""),

    # Indexes
    ("idx chunks(page_id)",         f"CREATE INDEX IF NOT EXISTS idx_chunks_page_id    ON {SCHEMA}.wiki_chunks(page_id);"),
    ("idx images(page_id)",         f"CREATE INDEX IF NOT EXISTS idx_images_page_id    ON {SCHEMA}.wiki_images(page_id);"),
    ("idx conversations(user)",     f"CREATE INDEX IF NOT EXISTS idx_conversations_user ON {SCHEMA}.conversations(user_id, updated_at DESC);"),
    ("idx messages(conv)",          f"CREATE INDEX IF NOT EXISTS idx_messages_conv      ON {SCHEMA}.messages(conversation_id, created_at);"),

    # HNSW vector index — ~95% recall, sub-ms latency
    ("HNSW cosine index",           f"""
        CREATE INDEX IF NOT EXISTS idx_embeddings_hnsw
        ON {SCHEMA}.wiki_embeddings
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64);"""),

    # Seed watermark
    ("seed sync watermark",         f"""
        INSERT INTO {SCHEMA}.sync_state (key, value)
        VALUES ('last_processed_rev_id', '0')
        ON CONFLICT (key) DO NOTHING;"""),

    # Grants for mediawiki role on wiki_rag schema
    ("grant schema usage",          f"GRANT USAGE ON SCHEMA {SCHEMA} TO {MW_ROLE};"),
    ("grant all tables",            f"GRANT ALL ON ALL TABLES IN SCHEMA {SCHEMA} TO {MW_ROLE};"),
    ("grant all sequences",         f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {SCHEMA} TO {MW_ROLE};"),
    ("default table privileges",    f"ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA} GRANT ALL ON TABLES TO {MW_ROLE};"),
    ("default sequence privileges", f"ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA} GRANT USAGE, SELECT ON SEQUENCES TO {MW_ROLE};"),
]
# fmt: on

with closing(_oauth_conn(DB_NAME)) as conn:
    with conn.cursor() as cur:
        for label, stmt in DDL:
            cur.execute(stmt)
            print(f"  ✅ {label}")

print("\n✅ DDL complete")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Store credentials in secret scope
# MAGIC 
# MAGIC Persists connection details into the secret scope (`mw_password` is already stored by `make setup-secrets`).

# COMMAND ----------

# Scope was already created by `make setup-secrets`, but ensure it exists
try:
    w.secrets.create_scope(scope=SCOPE)
    print(f"✅ Scope '{SCOPE}' created")
except ResourceAlreadyExists:
    print(f"✅ Scope '{SCOPE}' already exists")

# Store Lakebase connection details (mw_password already exists from setup-secrets)
secrets = {
    "lakebase_instance_name": PROJECT_ID,
    "lakebase_host": HOST,
    "lakebase_port": DB_PORT,
    "lakebase_db": DB_NAME,
    "lakebase_user": CURRENT_USER,
    "mw_role": MW_ROLE,
}

for k, v in secrets.items():
    w.secrets.put_secret(scope=SCOPE, key=k, string_value=str(v))

print("✅ Secrets stored:")
for k, v in secrets.items():
    print(f"   {k:30s} = {v}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Verify setup (password auth)
# MAGIC 
# MAGIC Smoke test: connects as `mediawiki` role with password auth to confirm everything works.

# COMMAND ----------

with closing(
    psycopg2.connect(
        host=HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=MW_ROLE,
        password=MW_PASSWORD,
        sslmode="require",
        connect_timeout=30,
    )
) as conn:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s ORDER BY table_name",
            (SCHEMA,),
        )
        tables = [r[0] for r in cur.fetchall()]

        cur.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = %s ORDER BY indexname",
            (SCHEMA,),
        )
        indexes = [r[0] for r in cur.fetchall()]

        cur.execute("SELECT version();")
        pg_version = cur.fetchone()[0].split(",")[0]

print(f"📋 PostgreSQL: {pg_version}")
print(f"📋 Tables ({len(tables)}):  {', '.join(tables)}")
print(f"📋 Indexes ({len(indexes)}): {', '.join(indexes)}")
print(f"\n🎉 Lakebase is ready — {HOST}")
