# Databricks notebook source

# COMMAND ----------

# MAGIC %md
# MAGIC # 01 — Ingest MediaWiki
# MAGIC
# MAGIC Reads wikitext from MediaWiki's native PostgreSQL tables (on Lakebase),
# MAGIC cleans it, chunks it, generates embeddings via Foundation Model API,
# MAGIC and writes everything back to the `wiki_rag` schema.
# MAGIC
# MAGIC **Incremental** — only processes pages with `rev_id` greater than the last watermark.

# COMMAND ----------

# MAGIC %pip install psycopg2-binary pgvector mwparserfromhell langchain-text-splitters databricks-openai databricks-sdk --upgrade -q
# MAGIC %restart_python

# COMMAND ----------

# MAGIC %md
# MAGIC ## Setup

# COMMAND ----------

import os
import sys

sys.path.insert(0, os.path.join(os.getcwd(), ".."))

from psycopg2.extras import execute_values

from src.config import get_lakebase_conn
from src.ingestion.mediawiki_reader import fetch_pages
from src.pipeline.chunker import TextChunk, chunk_page
from src.pipeline.cleaner import clean_wikitext
from src.pipeline.embedder import embed_texts

conn = get_lakebase_conn()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Read watermark

# COMMAND ----------

with conn.cursor() as cur:
    cur.execute("SELECT value FROM wiki_rag.sync_state WHERE key = 'last_processed_rev_id'")
    watermark = int(cur.fetchone()[0])

print(f"Current watermark: rev_id = {watermark}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Fetch pages, clean, and chunk

# COMMAND ----------

all_chunks: list[TextChunk] = []
max_rev_id = watermark
page_count = 0

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

print(f"✓ {page_count} pages → {len(all_chunks)} chunks (max rev_id: {max_rev_id})")

# COMMAND ----------

if not all_chunks:
    print("No new pages to process.")
    dbutils.notebook.exit("No new pages")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Generate embeddings

# COMMAND ----------

chunk_texts = [c.text for c in all_chunks]
print(f"⏳ Embedding {len(chunk_texts)} chunks...")

embeddings = embed_texts(chunk_texts)
print(f"✓ {len(embeddings)} embeddings (dim={len(embeddings[0])})")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Write to Lakebase
# MAGIC
# MAGIC Deletes stale chunks for re-processed pages (cascades to embeddings via FK),
# MAGIC then inserts new chunks + embeddings in a single transaction.

# COMMAND ----------

page_ids = list({c.page_id for c in all_chunks})

conn.autocommit = False
try:
    with conn.cursor() as cur:
        # Remove stale data (FK cascade deletes associated embeddings)
        cur.execute(
            "DELETE FROM wiki_rag.wiki_chunks WHERE page_id = ANY(%s)",
            (page_ids,),
        )
        print(f"  Deleted {cur.rowcount} old chunks for {len(page_ids)} pages")

        # Insert chunks
        chunk_rows = [
            (c.page_id, c.page_title, c.page_ns, c.rev_id, c.chunk_index, c.text)
            for c in all_chunks
        ]
        chunk_ids = execute_values(
            cur,
            """INSERT INTO wiki_rag.wiki_chunks
                   (page_id, page_title, page_ns, rev_id, chunk_index, chunk_text)
               VALUES %s RETURNING chunk_id""",
            chunk_rows,
            fetch=True,
        )
        chunk_ids = [row[0] for row in chunk_ids]
        print(f"  Inserted {len(chunk_ids)} chunks")

        # Insert embeddings
        emb_rows = list(zip(chunk_ids, embeddings))
        execute_values(
            cur,
            "INSERT INTO wiki_rag.wiki_embeddings (chunk_id, embedding) VALUES %s",
            emb_rows,
            template="(%s, %s::vector)",
        )
        print(f"  Inserted {len(emb_rows)} embeddings")

        # Advance watermark
        cur.execute(
            "UPDATE wiki_rag.sync_state SET value = %s, updated_at = now() "
            "WHERE key = 'last_processed_rev_id'",
            (str(max_rev_id),),
        )

    conn.commit()
    print(f"\n✓ Watermark advanced to rev_id = {max_rev_id}")

except Exception:
    conn.rollback()
    raise

# COMMAND ----------

# MAGIC %md
# MAGIC ## Summary

# COMMAND ----------

with conn.cursor() as cur:
    cur.execute("SELECT COUNT(*) FROM wiki_rag.wiki_chunks")
    total_chunks = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM wiki_rag.wiki_embeddings")
    total_embeddings = cur.fetchone()[0]

    cur.execute("SELECT value FROM wiki_rag.sync_state WHERE key = 'last_processed_rev_id'")
    current_wm = cur.fetchone()[0]

print(f"Total chunks:     {total_chunks}")
print(f"Total embeddings: {total_embeddings}")
print(f"Watermark:        rev_id = {current_wm}")

# COMMAND ----------

conn.close()
print("✓ Done")
