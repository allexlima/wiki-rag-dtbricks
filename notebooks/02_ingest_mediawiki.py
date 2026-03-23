# Databricks notebook source

# COMMAND ----------

# MAGIC %md
# MAGIC # 02 — Ingest MediaWiki
# MAGIC
# MAGIC Incremental, multimodal ingestion: reads wikitext from MediaWiki's Lakebase tables,
# MAGIC cleans/chunks text, captions images via vision LLM, embeds everything, and writes
# MAGIC back to the `wiki_rag` schema. Only processes revisions past the stored watermark.
# MAGIC
# MAGIC > **Prerequisites:** Run `00_setup_lakebase` first.

# COMMAND ----------

# MAGIC %pip install databricks-langchain psycopg2-binary pgvector mwparserfromhell tenacity Pillow -q
# MAGIC %restart_python

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration

# COMMAND ----------

import logging
import os
import sys

try:
    dbutils  # noqa: F821
except NameError:
    dbutils = None  # type: ignore[assignment]

# ─── Path handling ────────────────────────────────────────────────────
# DAB deploys notebooks/ and src/ as siblings under .bundle/.../files/
# Notebook CWD may point to notebooks/, so go up one level to reach the
# bundle root where src/ lives alongside notebooks/
_cwd = os.getcwd()
if os.path.basename(_cwd) == "notebooks":
    BUNDLE_ROOT = os.path.dirname(_cwd)
else:
    BUNDLE_ROOT = _cwd
sys.path.insert(0, BUNDLE_ROOT)

# ─── Widgets (defaults from databricks.yml) ──────────────────────────
from src.config import load_bundle_defaults
_defaults = load_bundle_defaults()

dbutils.widgets.text("secret_scope", _defaults["secret_scope"], "Secret Scope")
dbutils.widgets.text("embedding_model", _defaults["embedding_model"], "Embedding Model")
dbutils.widgets.text("llm_model", _defaults["llm_model"], "Vision LLM")
dbutils.widgets.text("mediawiki_url", _defaults.get("mediawiki_url", "http://localhost:8080"), "MediaWiki URL")

# ─── Read parameters ─────────────────────────────────────────────────
SECRET_SCOPE = dbutils.widgets.get("secret_scope")
EMBEDDING_MODEL = dbutils.widgets.get("embedding_model")
LLM_MODEL = dbutils.widgets.get("llm_model")
MEDIAWIKI_URL = dbutils.widgets.get("mediawiki_url")

# ─── Validate parameters ─────────────────────────────────────────────
assert SECRET_SCOPE and SECRET_SCOPE.strip(), (
    "Widget 'secret_scope' is required — set it via the widget bar or DAB job parameters."
)
assert EMBEDDING_MODEL and EMBEDDING_MODEL.strip(), (
    "Widget 'embedding_model' is required — provide a Foundation Model API endpoint name."
)
assert LLM_MODEL and LLM_MODEL.strip(), (
    "Widget 'llm_model' is required — provide a vision-capable LLM endpoint name."
)

# Propagate models to environment so WikiPipeline picks them up via os.environ
os.environ["EMBEDDING_MODEL"] = EMBEDDING_MODEL
os.environ["VISION_MODEL"] = LLM_MODEL

print(f"Secret scope:    {SECRET_SCOPE}")
print(f"Embedding model: {EMBEDDING_MODEL}")
print(f"Vision LLM:      {LLM_MODEL}")
print(f"MediaWiki URL:   {MEDIAWIKI_URL}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Connect to Lakebase

# COMMAND ----------

from contextlib import closing

from psycopg2.extras import execute_values

from src.config import get_lakebase_conn
from src.ingestion import MediaWikiIngestion
from src.pipeline import TextChunk, WikiPipeline

log = logging.getLogger(__name__)

ingestion = MediaWikiIngestion()
pipeline = WikiPipeline()

conn = get_lakebase_conn()
print(f"Connected to Lakebase (server={conn.info.host})")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Read watermark

# COMMAND ----------

with conn.cursor() as cur:
    cur.execute("SELECT value FROM wiki_rag.sync_state WHERE key = 'last_processed_rev_id'")
    row = cur.fetchone()
    assert row is not None, (
        "Watermark row not found in wiki_rag.sync_state. "
        "Run 00_setup_lakebase first to seed the sync_state table."
    )
    watermark = int(row[0])

print(f"Current watermark: rev_id = {watermark}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Fetch pages, clean, chunk, and process images
# MAGIC
# MAGIC Processes each new page: clean wikitext, chunk text, extract and caption images.

# COMMAND ----------

all_chunks: list[TextChunk] = []
image_records: list[tuple] = []  # (page_id, page_title, filename, alt_text, caption)
max_rev_id = watermark
page_count = 0

for page in ingestion.fetch_pages(conn, watermark):
    clean_text = pipeline.clean_wikitext(page.wikitext)

    text_chunks = pipeline.chunk_page(
        page_id=page.page_id,
        page_title=page.page_title,
        page_ns=page.page_ns,
        rev_id=page.rev_id,
        clean_text=clean_text,
    ) if clean_text.strip() else []
    all_chunks.extend(text_chunks)

    # Extract image refs from raw wikitext (before cleaning strips them)
    image_refs = pipeline.extract_image_refs(page.wikitext)
    for ref in image_refs:
        try:
            image_bytes = pipeline.fetch_image_from_mediawiki(ref.filename, base_url=MEDIAWIKI_URL)
            if image_bytes is None:
                continue

            caption = pipeline.caption_image(
                image_bytes=image_bytes,
                alt_text=ref.alt_text,
                page_title=page.page_title,
                filename=ref.filename,
            )

            image_records.append((
                page.page_id, page.page_title, ref.filename, ref.alt_text, caption,
            ))

            # Offset avoids index collisions with text chunks from the same page
            img_chunks = pipeline.chunk_image_caption(
                page_id=page.page_id,
                page_title=page.page_title,
                page_ns=page.page_ns,
                rev_id=page.rev_id,
                filename=ref.filename,
                caption=caption,
                chunk_index_offset=len(text_chunks),
            )
            all_chunks.extend(img_chunks)
            print(f"  📷 {ref.filename}: captioned ({len(caption)} chars)")

        except Exception:
            log.warning("Failed to process image '%s' on page '%s'", ref.filename, page.page_title, exc_info=True)

    max_rev_id = max(max_rev_id, page.rev_id)
    page_count += 1

print(f"✓ {page_count} pages → {len(all_chunks)} chunks ({len(image_records)} images)")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Early exit — no new pages

# COMMAND ----------

if not all_chunks:
    print("No new pages to process.")
    dbutils.notebook.exit("No new pages")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Generate embeddings

# COMMAND ----------

chunk_texts = [c.text for c in all_chunks]
print(f"⏳ Embedding {len(chunk_texts)} chunks via '{EMBEDDING_MODEL}'...")

embeddings = pipeline.embed_texts(chunk_texts)
print(f"✓ {len(embeddings)} embeddings (dim={len(embeddings[0])})")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Write to Lakebase
# MAGIC
# MAGIC Single transaction: delete stale data, insert chunks + embeddings + images, advance watermark.

# COMMAND ----------

page_ids = list({c.page_id for c in all_chunks})

conn.autocommit = False
try:
    with conn.cursor() as cur:
        # FK cascade deletes associated embeddings automatically
        cur.execute(
            "DELETE FROM wiki_rag.wiki_chunks WHERE page_id = ANY(%s)",
            (page_ids,),
        )
        print(f"  Deleted {cur.rowcount} old chunks for {len(page_ids)} pages")

        cur.execute(
            "DELETE FROM wiki_rag.wiki_images WHERE page_id = ANY(%s)",
            (page_ids,),
        )
        print(f"  Deleted {cur.rowcount} old image records")

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

        emb_rows = list(zip(chunk_ids, embeddings))
        execute_values(
            cur,
            "INSERT INTO wiki_rag.wiki_embeddings (chunk_id, embedding) VALUES %s",
            emb_rows,
            template="(%s, %s::vector)",
        )
        print(f"  Inserted {len(emb_rows)} embeddings")

        if image_records:
            execute_values(
                cur,
                """INSERT INTO wiki_rag.wiki_images
                       (page_id, page_title, filename, alt_text, caption)
                   VALUES %s""",
                image_records,
            )
            print(f"  Inserted {len(image_records)} image records")

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
# MAGIC ## 6. Summary

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
