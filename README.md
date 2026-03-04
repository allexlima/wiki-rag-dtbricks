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

This provisions a Lakebase Provisioned instance, creates the `wikidb` database, stores credentials in the `wiki-rag` secret scope, and runs all DDL (pgvector extension, `wiki_rag` schema, tables, indexes).

After it completes, verify you can retrieve the credentials:

```bash
databricks secrets get-secret wiki-rag lakebase_host
databricks secrets get-secret wiki-rag lakebase_user
```

### Step 2 — Generate a Lakebase OAuth token

MediaWiki needs a password to connect. Generate a short-lived token:

```bash
databricks database generate-database-credential \
  --instance-names wiki-rag-lakebase \
  --output json | jq -r '.token'
```

> **Note:** This token expires after ~1 hour. For long-running MediaWiki usage, you'll need to regenerate it periodically.

### Step 3 — Configure and start MediaWiki

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
LAKEBASE_USER=<your Databricks email>
LAKEBASE_PASSWORD=<token from Step 2>
MW_SECRET_KEY=<openssl rand -hex 32>
MW_UPGRADE_KEY=<openssl rand -hex 16>
```

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

### Step 4 — Ingest, chunk, and embed

Run on your Databricks workspace:

```
notebooks/01_ingest_mediawiki.py
```

This reads MediaWiki's native `mediawiki.page` / `mediawiki.revision` / `mediawiki.text` tables directly from Lakebase, cleans the wikitext markup, chunks the text, generates embeddings via the Foundation Model API, and writes everything to `wiki_rag.wiki_chunks` and `wiki_rag.wiki_embeddings`.

It's **incremental** — only processes pages with `rev_id` greater than the stored watermark. Safe to re-run after adding new wiki content.

### Step 5 — Test the RAG agent

Run interactively on your Databricks workspace:

```
notebooks/02_rag_agent.py
```

Test the retriever in isolation, then the full LangGraph agent (retrieve → grade → rewrite → generate). Modify the `QUESTION` variable to try your own queries.

### Step 6 — Deploy the serving endpoint

Run on your Databricks workspace:

```
notebooks/03_deploy_serving.py
```

This logs the `WikiRAGModel` PyFunc to MLflow, registers it in Unity Catalog (`main.wiki_rag.wiki_rag_agent`), and creates a Model Serving endpoint (`wiki-rag-endpoint`) with scale-to-zero enabled.

The endpoint environment variables are wired to the `wiki-rag` secret scope automatically.

### Step 7 — Deploy the Streamlit chat UI

```bash
databricks apps create wiki-rag-app --source-code-path app/
```

Or deploy with the DAB bundle:

```bash
databricks bundle deploy
```

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

You can inspect the data using pgAdmin, DBeaver, or VS Code SQLTools:

| Field | Value |
|-------|-------|
| Host | `databricks secrets get-secret wiki-rag lakebase_host` |
| Port | `5432` |
| Database | `wikidb` |
| Username | Your Databricks email |
| Password | `databricks database generate-database-credential --instance-names wiki-rag-lakebase` |
| SSL Mode | `require` |

> The password is a short-lived OAuth token (~1 hour). Regenerate as needed.

## Secret Scope Reference

All credentials are stored in the `wiki-rag` Databricks secret scope:

| Key | Description |
|-----|-------------|
| `lakebase_instance_name` | Lakebase instance name (`wiki-rag-lakebase`) |
| `lakebase_user` | Databricks username (email) |
| `lakebase_db` | Database name (`wikidb`) |
| `lakebase_host` | Lakebase endpoint DNS |
