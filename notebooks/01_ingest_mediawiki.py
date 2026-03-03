# Databricks notebook source
# MAGIC %md
# MAGIC # 01 — Ingest MediaWiki → Clean → Chunk → Embed → Store
# MAGIC
# MAGIC Reads wikitext from MediaWiki's native PostgreSQL tables (on Lakebase),
# MAGIC cleans it, chunks it, generates embeddings via Foundation Model API,
# MAGIC and writes everything back to the `wiki_rag` schema in Lakebase.
# MAGIC
# MAGIC **Incremental**: only processes pages with rev_id > last watermark.

# COMMAND ----------

# MAGIC %pip install psycopg2-binary pgvector mwparserfromhell langchain-text-splitters databricks-openai databricks-sdk --upgrade -q
# MAGIC %restart_python

# COMMAND ----------

import sys
import os

# Add src/ to path so we can import our modules
sys.path.insert(0, os.path.join(os.getcwd(), ".."))

from src.ingestion.mediawiki_reader import fetch_pages
from src.pipeline.cleaner import clean_wikitext
from src.pipeline.chunker import chunk_page
from src.pipeline.embedder import embed_texts

# COMMAND ----------

import uuid
import psycopg2
from psycopg2.extras import execute_values
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
# MAGIC ## Read watermark

# COMMAND ----------

with get_conn() as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM wiki_rag.sync_state WHERE key = 'last_processed_rev_id'")
        watermark = int(cur.fetchone()[0])

print(f"Current watermark: rev_id = {watermark}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Fetch pages → Clean → Chunk

# COMMAND ----------

from src.pipeline.chunker import TextChunk

all_chunks: list[TextChunk] = []
max_rev_id = watermark
page_count = 0

with get_conn() as conn:
    for page in fetch_pages(conn, watermark):
        clean_text = clean_wikitext(page.wikitext)
        if not clean_text.strip():
            continue

        chunks = chunk_page(
            page_id=page.page_id,
            page_title=page.page_title,
            page_ns=page.page_ns,
            rev_id=page.rev_id,
            clean_text=clean_text,
        )
        all_chunks.extend(chunks)
        max_rev_id = max(max_rev_id, page.rev_id)
        page_count += 1

print(f"Processed {page_count} pages → {len(all_chunks)} chunks (max rev_id: {max_rev_id})")

# COMMAND ----------

if not all_chunks:
    print("No new pages to process. Exiting.")
    dbutils.notebook.exit("No new pages")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Generate embeddings

# COMMAND ----------

chunk_texts = [c.text for c in all_chunks]
print(f"Embedding {len(chunk_texts)} chunks...")

embeddings = embed_texts(chunk_texts)
print(f"Generated {len(embeddings)} embeddings (dim={len(embeddings[0])})")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Write to Lakebase
# MAGIC
# MAGIC Delete old data for re-processed pages, then insert new chunks + embeddings.

# COMMAND ----------

# Collect page_ids that we're re-processing
page_ids = list({c.page_id for c in all_chunks})

with get_conn() as conn:
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            # Delete old chunks (cascades to embeddings via FK)
            cur.execute(
                "DELETE FROM wiki_rag.wiki_chunks WHERE page_id = ANY(%s)",
                (page_ids,),
            )
            deleted = cur.rowcount
            print(f"Deleted {deleted} old chunks for {len(page_ids)} pages")

            # Insert new chunks
            chunk_rows = [
                (c.page_id, c.page_title, c.page_ns, c.rev_id, c.chunk_index, c.text)
                for c in all_chunks
            ]
            insert_sql = """
                INSERT INTO wiki_rag.wiki_chunks
                    (page_id, page_title, page_ns, rev_id, chunk_index, chunk_text)
                VALUES %s
                RETURNING chunk_id
            """
            chunk_ids = execute_values(cur, insert_sql, chunk_rows, fetch=True)
            chunk_ids = [row[0] for row in chunk_ids]
            print(f"Inserted {len(chunk_ids)} chunks")

            # Insert embeddings
            emb_rows = list(zip(chunk_ids, embeddings))
            execute_values(
                cur,
                "INSERT INTO wiki_rag.wiki_embeddings (chunk_id, embedding) VALUES %s",
                emb_rows,
                template="(%s, %s::vector)",
            )
            print(f"Inserted {len(emb_rows)} embeddings")

            # Update watermark
            cur.execute(
                """
                UPDATE wiki_rag.sync_state
                SET value = %s, updated_at = now()
                WHERE key = 'last_processed_rev_id'
                """,
                (str(max_rev_id),),
            )

        conn.commit()
        print(f"Watermark updated to rev_id = {max_rev_id}")

    except Exception:
        conn.rollback()
        raise

# COMMAND ----------

# MAGIC %md
# MAGIC ## Summary

# COMMAND ----------

with get_conn() as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM wiki_rag.wiki_chunks")
        total_chunks = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM wiki_rag.wiki_embeddings")
        total_embeddings = cur.fetchone()[0]
        cur.execute("SELECT value FROM wiki_rag.sync_state WHERE key = 'last_processed_rev_id'")
        current_wm = cur.fetchone()[0]

print(f"Total chunks: {total_chunks}")
print(f"Total embeddings: {total_embeddings}")
print(f"Current watermark: {current_wm}")
