# Databricks notebook source

# COMMAND ----------

# MAGIC %md
# MAGIC # 02 — Ingest MediaWiki
# MAGIC 
# MAGIC Incremental multimodal ingestion pipeline:
# MAGIC 1. Fetch new pages from MediaWiki (via Lakebase native tables)
# MAGIC 2. Caption images via vision LLM (parallel) and inline them into the text
# MAGIC 3. Chunk the enriched text and generate embeddings
# MAGIC 4. Write everything to `wiki_rag` schema in a single transaction
# MAGIC 
# MAGIC > **Prerequisites:** `00_setup_lakebase` + MediaWiki with content loaded.

# COMMAND ----------

# MAGIC %pip install databricks-langchain psycopg2-binary pgvector mwparserfromhell tenacity Pillow tqdm -q
# MAGIC %restart_python

# COMMAND ----------

# MAGIC %md
# MAGIC ## Setup: imports, config, and constants

# COMMAND ----------

import logging
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from psycopg2.extras import execute_values
from tqdm import tqdm

# Bundle root resolution (notebooks/ and src/ are siblings under .bundle/.../files/)
_cwd = os.getcwd()
BUNDLE_ROOT = os.path.dirname(_cwd) if os.path.basename(_cwd) == "notebooks" else _cwd
sys.path.insert(0, BUNDLE_ROOT)

from src.config import get_lakebase_conn, load_bundle_defaults
from src.ingestion import MediaWikiIngestion
from src.pipeline import TextChunk, WikiPipeline

log = logging.getLogger(__name__)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration

# COMMAND ----------

_defaults = load_bundle_defaults()

dbutils.widgets.text("secret_scope", _defaults["secret_scope"], "Secret Scope")
dbutils.widgets.text("embedding_model", _defaults["embedding_model"], "Embedding Model")
dbutils.widgets.text("llm_model", _defaults["llm_model"], "Vision LLM")
dbutils.widgets.text(
    "mediawiki_url",
    _defaults.get("mediawiki_url", "http://localhost:8080"),
    "MediaWiki URL",
)

# COMMAND ----------

SECRET_SCOPE = dbutils.widgets.get("secret_scope")
EMBEDDING_MODEL = dbutils.widgets.get("embedding_model")
LLM_MODEL = dbutils.widgets.get("llm_model")
MEDIAWIKI_URL = dbutils.widgets.get("mediawiki_url")

MAX_WORKERS = 10  # parallel threads for captioning + embedding

os.environ["EMBEDDING_MODEL"] = EMBEDDING_MODEL
os.environ["VISION_MODEL"] = LLM_MODEL

print(f"Scope: {SECRET_SCOPE}  |  Embed: {EMBEDDING_MODEL}  |  Vision: {LLM_MODEL}")
print(
    f"MediaWiki: {MEDIAWIKI_URL}  | Workers for captioning + embedding: {MAX_WORKERS}"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Pre-flight: connectivity check
# MAGIC 
# MAGIC Fails fast if this compute can't reach MediaWiki (images won't be captioned).
# MAGIC If blocked, check [Databricks IP ranges](https://www.databricks.com/networking/v1/ip-ranges.json)
# MAGIC and add the relevant CIDR blocks to the MediaWiki ALB security group or firewall.

# COMMAND ----------

try:
    _resp = requests.get(
        f"{MEDIAWIKI_URL}/api.php?action=query&meta=siteinfo&format=json", timeout=10
    )
    _site = _resp.json().get("query", {}).get("general", {}).get("sitename", "?")
    print(f"✅ MediaWiki reachable: {_site}")
    MW_REACHABLE = True
except Exception:
    MW_REACHABLE = False
    try:
        _my_ip = requests.get("https://checkip.amazonaws.com", timeout=5).text.strip()
    except Exception:
        _my_ip = "unknown"
    print(f"⚠️  Cannot reach MediaWiki at {MEDIAWIKI_URL}")
    print(f"   This compute's outbound IP: {_my_ip}")
    print(f"   → Add {_my_ip}/32 to the MediaWiki ALB security group.")
    print(
        "   → Find Databricks IP ranges: https://www.databricks.com/networking/v1/ip-ranges.json"
    )
    print("   Text ingestion will proceed; image captioning will be skipped.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Connect to Lakebase + read watermark
# MAGIC 
# MAGIC Incremental processing: `sync_state.last_processed_rev_id` tracks the highest
# MAGIC revision already ingested. Only pages with `rev_id > watermark` are fetched,
# MAGIC so re-running this notebook is safe and skips already-processed content.

# COMMAND ----------

ingestion = MediaWikiIngestion()
pipeline = WikiPipeline()

conn = get_lakebase_conn()
print(f"Connected to Lakebase ({conn.info.host})")

# Watermark = highest rev_id already ingested. Pages with rev_id > watermark are new.
with conn.cursor() as cur:
    cur.execute(
        "SELECT value FROM wiki_rag.sync_state WHERE key = 'last_processed_rev_id'"
    )
    row = cur.fetchone()
    assert row, "Watermark not found — run 00_setup_lakebase first."
    watermark = int(row[0])

print(
    f"Watermark: rev_id = {watermark} (pages with rev_id > {watermark} will be processed)"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Fetch pages and extract image refs

# COMMAND ----------

pages = list(ingestion.fetch_pages(conn, watermark))

# Build image task list per page for parallel captioning
image_tasks_by_page = {}  # page_id → [(page, ref), ...]
for page in pages:
    refs = pipeline.extract_image_refs(page.wikitext)
    if refs:
        image_tasks_by_page[page.page_id] = [(page, ref) for ref in refs]

total_images = sum(len(v) for v in image_tasks_by_page.values())
print(f"Found {len(pages)} pages, {total_images} images")

if not pages:
    print("No new pages to process.")
    dbutils.notebook.exit("No new pages")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Caption images
# MAGIC 
# MAGIC Fetches images from MediaWiki and generates captions via vision LLM.
# MAGIC Runs in `MAX_WORKERS` threads. Results are keyed by `(page_id, filename)`
# MAGIC so they can be inlined into the wikitext before chunking.

# COMMAND ----------

captions: dict[tuple[int, str], str] = {}  # (page_id, filename) → caption
image_records: list[tuple] = []


def _caption_one(page, ref) -> tuple[int, str, str, str, str] | None:
    """Fetch + caption a single image. Returns (page_id, title, filename, alt, caption) or None."""
    image_bytes = pipeline.fetch_image_from_mediawiki(
        ref.filename, base_url=MEDIAWIKI_URL
    )
    if image_bytes is None:
        return None
    caption = pipeline.caption_image(
        image_bytes=image_bytes,
        alt_text=ref.alt_text,
        page_title=page.page_title,
        filename=ref.filename,
    )
    return (page.page_id, page.page_title, ref.filename, ref.alt_text, caption)


all_tasks = [(p, r) for tasks in image_tasks_by_page.values() for p, r in tasks]

if MW_REACHABLE and all_tasks:
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_caption_one, p, r): (p, r) for p, r in all_tasks}
        for future in tqdm(
            as_completed(futures), total=len(futures), desc="🖼️  Captioning", unit="img"
        ):
            page, ref = futures[future]
            try:
                result = future.result()
                if result:
                    pid, title, fname, alt, cap = result
                    captions[(pid, fname)] = cap
                    image_records.append(result)
            except Exception:
                log.warning(
                    "Image '%s' on '%s' failed",
                    ref.filename,
                    page.page_title,
                    exc_info=True,
                )

    print(f"✅ {len(captions)} images captioned")
elif not MW_REACHABLE:
    print("⏭️  Skipping image captioning (MediaWiki unreachable)")
else:
    print("⏭️  No images to caption")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Enrich text with captions, then chunk
# MAGIC 
# MAGIC Replaces `[[File:...]]` references with `Image(source="...", caption="...")` blocks
# MAGIC at their original position, preserving document structure. The enriched text is
# MAGIC then chunked — so image descriptions live alongside their surrounding context.

# COMMAND ----------

all_chunks: list[TextChunk] = []
max_rev_id = watermark

for page in tqdm(pages, desc="📄 Chunking", unit="page"):
    # Replace [[File:...]] wikilinks with Image() placeholders in the RAW wikitext
    # BEFORE cleaning — because clean_wikitext() strips all [[File:...]] links entirely.
    enriched_wikitext = page.wikitext
    for ref in pipeline.extract_image_refs(page.wikitext):
        caption = captions.get((page.page_id, ref.filename))
        if caption:
            placeholder = f'Image(source="{ref.filename}", caption="{caption}")'
        else:
            placeholder = ""
        # Replace the full [[File:name|...]] wikilink with our plain-text placeholder
        pattern = r"\[\[(File|Image):" + re.escape(ref.filename) + r"[^\]]*\]\]"
        enriched_wikitext = re.sub(
            pattern, placeholder, enriched_wikitext, flags=re.IGNORECASE
        )

    clean_text = pipeline.clean_wikitext(enriched_wikitext)

    chunks = (
        pipeline.chunk_page(
            page_id=page.page_id,
            page_title=page.page_title,
            page_ns=page.page_ns,
            rev_id=page.rev_id,
            clean_text=clean_text,
        )
        if clean_text.strip()
        else []
    )

    # Mark chunks containing image captions for metadata tracking
    for c in chunks:
        if "Image(source=" in c.text:
            c.chunk_source = "image"

    all_chunks.extend(chunks)
    max_rev_id = max(max_rev_id, page.rev_id)

image_chunk_count = sum(1 for c in all_chunks if c.chunk_source == "image")
print(f"✅ {len(all_chunks)} chunks ({image_chunk_count} contain image captions)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6. Generate embeddings

# COMMAND ----------

if not all_chunks:
    print("No chunks to embed.")
    dbutils.notebook.exit("No chunks")

chunk_texts = [c.text for c in all_chunks]
batch_size = pipeline.EMBEDDING_BATCH_SIZE
batches = [
    chunk_texts[i : i + batch_size] for i in range(0, len(chunk_texts), batch_size)
]

print(
    f"Embedding {len(chunk_texts)} chunks in {len(batches)} batches ({MAX_WORKERS} workers)..."
)

# Parallel embedding: each batch is an independent API call
embeddings_by_batch: dict[int, list] = {}
with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
    futures = {
        pool.submit(pipeline.embed_texts, batch): idx
        for idx, batch in enumerate(batches)
    }
    for future in tqdm(
        as_completed(futures), total=len(futures), desc="🧮 Embedding", unit="batch"
    ):
        idx = futures[future]
        embeddings_by_batch[idx] = future.result()

# Reassemble in original order
embeddings = []
for idx in range(len(batches)):
    embeddings.extend(embeddings_by_batch[idx])

print(f"✅ {len(embeddings)} embeddings (dim={len(embeddings[0])})")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7. Write to Lakebase
# MAGIC 
# MAGIC Single atomic transaction: delete stale data → insert chunks + embeddings + images → advance watermark.

# COMMAND ----------

page_ids = list({c.page_id for c in all_chunks})

if conn.info.transaction_status != 0:
    conn.commit()
conn.autocommit = False

try:
    with conn.cursor() as cur:
        # FK cascade deletes associated embeddings
        cur.execute(
            "DELETE FROM wiki_rag.wiki_chunks WHERE page_id = ANY(%s)", (page_ids,)
        )
        cur.execute(
            "DELETE FROM wiki_rag.wiki_images WHERE page_id = ANY(%s)", (page_ids,)
        )

        chunk_rows = [
            (
                c.page_id,
                c.page_title,
                c.page_ns,
                c.rev_id,
                c.chunk_index,
                c.text,
                c.chunk_source,
            )
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

        execute_values(
            cur,
            "INSERT INTO wiki_rag.wiki_embeddings (chunk_id, embedding) VALUES %s",
            list(zip(chunk_ids, embeddings)),
            template="(%s, %s::vector)",
        )

        if image_records:
            execute_values(
                cur,
                """INSERT INTO wiki_rag.wiki_images
                       (page_id, page_title, filename, alt_text, caption)
                   VALUES %s""",
                image_records,
            )

        cur.execute(
            "UPDATE wiki_rag.sync_state SET value = %s, updated_at = now() WHERE key = 'last_processed_rev_id'",
            (str(max_rev_id),),
        )

    conn.commit()
    print(
        f"✅ Written {len(chunk_ids)} chunks + {len(embeddings)} embeddings + {len(image_records)} images"
    )
    print(f"   Watermark → rev_id = {max_rev_id}")

except Exception:
    conn.rollback()
    raise

# COMMAND ----------

# MAGIC %md
# MAGIC ## 8. Summary

# COMMAND ----------

with conn.cursor() as cur:
    cur.execute("SELECT COUNT(*) FROM wiki_rag.wiki_chunks")
    total_chunks = cur.fetchone()[0]
    cur.execute(
        "SELECT COUNT(*) FROM wiki_rag.wiki_chunks WHERE chunk_source = 'image'"
    )
    img_chunks = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM wiki_rag.wiki_embeddings")
    total_emb = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM wiki_rag.wiki_images")
    total_img = cur.fetchone()[0]
    cur.execute(
        "SELECT value FROM wiki_rag.sync_state WHERE key = 'last_processed_rev_id'"
    )
    wm = cur.fetchone()[0]

conn.close()

print(f"Chunks:     {total_chunks} ({img_chunks} with image captions)")
print(f"Embeddings: {total_emb}")
print(f"Images:     {total_img}")
print(f"Watermark:  rev_id = {wm}")
print("✓ Done")
