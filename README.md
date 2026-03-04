# Wiki RAG on Databricks

End-to-end Retrieval-Augmented Generation (RAG) system that turns a self-hosted MediaWiki into an intelligent Q&A assistant, powered entirely by Databricks.

![Architecture](docs/assets/architecture.png)

## Architecture

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Knowledge source | MediaWiki 1.42 (Docker) | Self-hosted wiki with PostgreSQL backend |
| Database | Lakebase Provisioned (PG 17) | Single instance hosting both MW tables and RAG tables |
| Embeddings | `databricks-gte-large-en` (1024-dim) | Foundation Model API for text embeddings |
| Vector search | pgvector HNSW index | Cosine similarity retrieval |
| RAG agent | LangGraph StateGraph | retrieve → grade → rewrite loop → generate |
| LLM | `databricks-meta-llama-3-3-70b-instruct` | Answer generation |
| Serving | MLflow PyFunc + Model Serving | Real-time endpoint with OAuth rotation |
| Chat UI | Streamlit (Databricks App) | Web interface calling the serving endpoint |

## Project Structure

```
wiki-rag-dtbricks/
├── databricks.yml              # DAB bundle config
├── resources/
│   ├── jobs.yml                # Workflow definitions
│   ├── serving.yml             # Serving endpoint config
│   └── apps.yml                # Databricks App config
├── docker/
│   ├── docker-compose.yml      # MediaWiki container
│   ├── LocalSettings.php.template
│   ├── .env.example            # Lakebase credentials template
│   └── setup.sh                # One-command MediaWiki bootstrap
├── src/
│   ├── config.py               # Shared Lakebase connection helper
│   ├── ingestion/
│   │   └── mediawiki_reader.py # Reads MW native PG tables
│   ├── pipeline/
│   │   ├── cleaner.py          # Strips wikitext markup
│   │   ├── chunker.py          # RecursiveCharacterTextSplitter
│   │   └── embedder.py         # Foundation Model API embeddings
│   ├── rag/
│   │   ├── retriever.py        # pgvector cosine search
│   │   └── agent.py            # LangGraph RAG agent
│   └── serving/
│       └── pyfunc_model.py     # MLflow PyFunc wrapper
├── notebooks/
│   ├── 00_setup_lakebase.py    # Provision Lakebase + schema DDL
│   ├── 01_ingest_mediawiki.py  # Ingest → clean → chunk → embed
│   ├── 02_rag_agent.py         # Interactive RAG testing
│   └── 03_deploy_serving.py    # Register model + deploy endpoint
└── app/
    ├── app.py                  # Streamlit chat UI
    ├── app.yaml                # Databricks App config
    └── requirements.txt
```

## Prerequisites

- Databricks workspace with Unity Catalog enabled
- Databricks CLI (`>= 0.236.0`) authenticated to your workspace
- Docker and Docker Compose (for MediaWiki)
- Python 3.11+

## Setup

### Step 1 — Provision Lakebase and create the schema

Run the setup notebook on your Databricks workspace:

```
notebooks/00_setup_lakebase.py
```

This notebook:
1. Provisions a Lakebase Provisioned instance
2. Creates the `wikidb` database
3. Enables **native PG login** and creates a `mediawiki` role with a static password (no token expiration)
4. Stores all credentials in the `wiki-rag` secret scope
5. Runs all DDL (pgvector extension, `wiki_rag` schema, tables, indexes)

> **Important:** Set the `mw_password` widget before running — this becomes the static password for the `mediawiki` PostgreSQL role used by the Docker container.

After it completes, verify the credentials:

```bash
databricks secrets get-secret wiki-rag lakebase_host
databricks secrets get-secret wiki-rag mw_role
```

### Step 2 — Configure and start MediaWiki

```bash
cd docker

# Create .env from template
cp .env.example .env
```

Edit `docker/.env` with your Lakebase credentials:

```env
LAKEBASE_HOST=<output of: databricks secrets get-secret wiki-rag lakebase_host>
LAKEBASE_PORT=5432
LAKEBASE_DB=wikidb
LAKEBASE_USER=mediawiki
LAKEBASE_PASSWORD=<the password you set in the mw_password widget>
MW_SECRET_KEY=<openssl rand -hex 32>
MW_UPGRADE_KEY=<openssl rand -hex 16>
```

> The `mediawiki` role uses native PG login with a static password — no token rotation needed.

Then run the bootstrap script:

```bash
chmod +x setup.sh
./setup.sh
```

This will:
1. Generate `LocalSettings.php` from the template with your credentials
2. Start the MediaWiki container (exposed at `http://localhost:8080`)
3. Run `php maintenance/run.php install` to create MediaWiki's native tables in the `mediawiki` schema on Lakebase
4. Run `php maintenance/run.php update` to ensure the schema is current

You can now access MediaWiki at **http://localhost:8080** (admin: `Admin` / `admin123`). Add some wiki pages — these will be ingested in the next step.

### Step 3 — Ingest, chunk, and embed

Run on your Databricks workspace:

```
notebooks/01_ingest_mediawiki.py
```

This reads MediaWiki's native `mediawiki.page` / `mediawiki.revision` / `mediawiki.text` tables directly from Lakebase, cleans the wikitext markup, chunks the text, generates embeddings via the Foundation Model API, and writes everything to `wiki_rag.wiki_chunks` and `wiki_rag.wiki_embeddings`.

It's **incremental** — only processes pages with `rev_id` greater than the stored watermark. Safe to re-run after adding new wiki content.

### Step 4 — Test the RAG agent

Run interactively on your Databricks workspace:

```
notebooks/02_rag_agent.py
```

Test the retriever in isolation, then the full LangGraph agent (retrieve → grade → rewrite → generate). Modify the `QUESTION` variable to try your own queries.

### Step 5 — Deploy the serving endpoint

Run on your Databricks workspace:

```
notebooks/03_deploy_serving.py
```

This logs the `WikiRAGModel` PyFunc to MLflow, registers it in Unity Catalog (`main.wiki_rag.wiki_rag_agent`), and creates a Model Serving endpoint (`wiki-rag-endpoint`) with scale-to-zero enabled.

The endpoint environment variables are wired to the `wiki-rag` secret scope automatically.

### Step 6 — Deploy the Streamlit chat UI and ingestion workflow

Deploy everything with the DAB bundle:

```bash
databricks bundle deploy
```

This deploys:
- **Ingestion workflow** (`wiki-rag-ingestion`) — scheduled hourly (paused by default), runs `01_ingest_mediawiki.py` on serverless compute to pick up new wiki edits incrementally
- **Streamlit app** reference for the chat UI

To deploy the Streamlit app separately:

```bash
databricks apps create wiki-rag-app --source-code-path app/
```

### Step 7 — Enable the ingestion schedule

The ingestion workflow is deployed **paused** so you can verify everything works first. Unpause it when ready:

```bash
# Find the job ID
databricks jobs list --name wiki-rag-ingestion

# Unpause the schedule
databricks jobs update <JOB_ID> --json '{"schedule": {"pause_status": "UNPAUSED"}}'
```

Or unpause from the Databricks **Workflows** UI. The job runs every hour, processes only new pages (incremental via watermark), and exits gracefully when there's nothing new.

## Database Schema

The single `wikidb` database on Lakebase hosts two schemas:

| Schema | Owner | Tables |
|--------|-------|--------|
| `mediawiki` | MediaWiki | `page`, `revision`, `text`, `slots`, `content`, ... (MW native) |
| `wiki_rag` | RAG pipeline | `wiki_chunks`, `wiki_embeddings`, `sync_state` |

```sql
-- Chunks: cleaned and split wiki text
wiki_rag.wiki_chunks (chunk_id, page_id, page_title, page_ns, rev_id, chunk_index, chunk_text, created_at)

-- Embeddings: 1024-dim vectors with HNSW index
wiki_rag.wiki_embeddings (embedding_id, chunk_id, embedding)

-- Sync state: watermark for incremental processing
wiki_rag.sync_state (key, value, updated_at)
```

## Connecting to Lakebase from a SQL client

You can inspect the data using pgAdmin, DBeaver, or VS Code SQLTools.

**With the `mediawiki` role (static password):**

| Field | Value |
|-------|-------|
| Host | `databricks secrets get-secret wiki-rag lakebase_host` |
| Port | `5432` |
| Database | `wikidb` |
| Username | `mediawiki` |
| Password | The password you set in the `mw_password` widget |
| SSL Mode | `require` |

**With your Databricks identity (OAuth token, expires ~1h):**

| Field | Value |
|-------|-------|
| Host | `databricks secrets get-secret wiki-rag lakebase_host` |
| Port | `5432` |
| Database | `wikidb` |
| Username | Your Databricks email |
| Password | `databricks database generate-database-credential --instance-names wiki-rag-lakebase` |
| SSL Mode | `require` |

## Secret Scope Reference

All credentials are stored in the `wiki-rag` Databricks secret scope:

| Key | Description |
|-----|-------------|
| `lakebase_instance_name` | Lakebase instance name (`wiki-rag-lakebase`) |
| `lakebase_user` | Databricks username (email) |
| `lakebase_db` | Database name (`wikidb`) |
| `lakebase_host` | Lakebase endpoint DNS |
| `mw_role` | MediaWiki PG role name (`mediawiki`) |
| `mw_password` | Static password for the `mediawiki` PG role |
