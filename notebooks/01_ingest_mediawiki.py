# Databricks notebook source

# COMMAND ----------

# MAGIC %md
# MAGIC # 01 — Ingest MediaWiki
# MAGIC
# MAGIC Reads wikitext from MediaWiki's native PostgreSQL tables (on Lakebase),
# MAGIC cleans it, chunks it, generates embeddings via Foundation Model API,
# MAGIC and writes everything back to the `wiki_rag` schema.
# MAGIC
# MAGIC **Multimodal:** extracts image references, fetches images from MediaWiki,
# MAGIC and generates text descriptions via a vision LLM.
# MAGIC
# MAGIC **Incremental** — only processes pages with `rev_id` greater than the last watermark.

# COMMAND ----------

# MAGIC %pip install psycopg2-binary pgvector mwparserfromhell langchain-text-splitters databricks-openai databricks-sdk tenacity Pillow --upgrade -q
# MAGIC %restart_python

# COMMAND ----------

# MAGIC %md
# MAGIC ## Setup

# COMMAND ----------

import logging
import os
import sys

sys.path.insert(0, os.path.join(os.getcwd(), ".."))

from psycopg2.extras import execute_values

from src.config import get_lakebase_conn
from src.ingestion import MediaWikiIngestion
from src.pipeline import TextChunk, WikiPipeline

log = logging.getLogger(__name__)

ingestion = MediaWikiIngestion()
pipeline = WikiPipeline()
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
# MAGIC ## Fetch pages, clean, chunk, and process images

# COMMAND ----------

all_chunks: list[TextChunk] = []
image_records: list[tuple] = []  # (page_id, page_title, filename, alt_text, caption)
max_rev_id = watermark
page_count = 0

for page in ingestion.fetch_pages(conn, watermark):
    clean_text = pipeline.clean_wikitext(page.wikitext)

    # --- Text chunks ---
    text_chunks = pipeline.chunk_page(
        page_id=page.page_id,
        page_title=page.page_title,
        page_ns=page.page_ns,
        rev_id=page.rev_id,
        clean_text=clean_text,
    ) if clean_text.strip() else []
    all_chunks.extend(text_chunks)

    # --- Image processing (multimodal) ---
    image_refs = pipeline.extract_image_refs(page.wikitext)
    for ref in image_refs:
        try:
            image_bytes = pipeline.fetch_image_from_mediawiki(ref.filename)
            if image_bytes is None:
                continue

            caption = pipeline.caption_image(
                image_bytes=image_bytes,
                alt_text=ref.alt_text,
                page_title=page.page_title,
            )

            # Track for DB insert
            image_records.append((
                page.page_id, page.page_title, ref.filename, ref.alt_text, caption,
            ))

            # Create image-sourced chunks
            img_chunks = pipeline.chunk_image_caption(
                page_id=page.page_id,
                page_title=page.page_title,
                page_ns=page.page_ns,
                rev_id=page.rev_id,
                filename=ref.filename,
                caption=caption,
                chunk_index_offset=len(text_chunks) + len(all_chunks),
            )
            all_chunks.extend(img_chunks)
            print(f"  📷 {ref.filename}: captioned ({len(caption)} chars)")

        except Exception:
            log.warning("Failed to process image '%s' on page '%s'", ref.filename, page.page_title, exc_info=True)

    max_rev_id = max(max_rev_id, page.rev_id)
    page_count += 1

print(f"✓ {page_count} pages → {len(all_chunks)} chunks ({len(image_records)} images)")

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

embeddings = pipeline.embed_texts(chunk_texts)
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

        # Remove stale image records
        cur.execute(
            "DELETE FROM wiki_rag.wiki_images WHERE page_id = ANY(%s)",
            (page_ids,),
        )
        print(f"  Deleted {cur.rowcount} old image records")

        # Insert chunks (with chunk_source)
        chunk_rows = [
            (c.page_id, c.page_title, c.page_ns, c.rev_id, c.chunk_index, c.text, c.chunk_source)
            for c in all_chunks
        ]
        chunk_ids = execute_values(
            cur,
            """INSERT INTO wiki_rag.wiki_chunks
                   (page_id, page_title, page_ns, rev_id, chunk_index, chunk_text, chunk_source)
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

        # Insert image metadata
        if image_records:
            execute_values(
                cur,
                """INSERT INTO wiki_rag.wiki_images
                       (page_id, page_title, filename, alt_text, caption)
                   VALUES %s""",
                image_records,
            )
            print(f"  Inserted {len(image_records)} image records")

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

    cur.execute("SELECT COUNT(*) FROM wiki_rag.wiki_chunks WHERE chunk_source = 'image'")
    image_chunks = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM wiki_rag.wiki_embeddings")
    total_embeddings = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM wiki_rag.wiki_images")
    total_images = cur.fetchone()[0]

    cur.execute("SELECT value FROM wiki_rag.sync_state WHERE key = 'last_processed_rev_id'")
    current_wm = cur.fetchone()[0]

print(f"Total chunks:     {total_chunks} ({image_chunks} from images)")
print(f"Total embeddings: {total_embeddings}")
print(f"Total images:     {total_images}")
print(f"Watermark:        rev_id = {current_wm}")

# COMMAND ----------

conn.close()
print("✓ Done")
