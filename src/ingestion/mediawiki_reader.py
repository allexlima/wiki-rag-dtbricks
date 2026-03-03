"""
Reads current wikitext from MediaWiki's native PostgreSQL tables (Lakebase).

Table relationships (MediaWiki 1.42 Multi-Content Revisions):
  page.page_latest → revision.rev_id
  → slots.slot_revision_id → content.content_id
  → SUBSTRING(content_address, 4) → pagecontent.old_id
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Generator

import psycopg2
from psycopg2.extras import RealDictCursor


@dataclass
class WikiPage:
    page_id: int
    page_title: str
    page_ns: int
    rev_id: int
    wikitext: str


# Fetch main-namespace pages updated after a watermark revision ID.
# MediaWiki on PostgreSQL uses "pagecontent" as the blob table name.
FETCH_PAGES_SQL = """
SELECT
    p.page_id,
    REPLACE(p.page_title, '_', ' ') AS page_title,
    p.page_namespace AS page_ns,
    r.rev_id,
    pc.old_text AS wikitext
FROM mediawiki.page p
JOIN mediawiki.revision r
    ON r.rev_id = p.page_latest
JOIN mediawiki.slots s
    ON s.slot_revision_id = r.rev_id
JOIN mediawiki.content c
    ON c.content_id = s.slot_content_id
JOIN mediawiki.pagecontent pc
    ON SUBSTRING(c.content_address FROM 4)::BIGINT = pc.old_id
WHERE p.page_namespace = 0
  AND r.rev_id > %(watermark)s
ORDER BY r.rev_id ASC;
"""


def fetch_pages(
    conn: psycopg2.extensions.connection,
    watermark_rev_id: int = 0,
) -> Generator[WikiPage, None, None]:
    """Yield WikiPage objects for all pages updated after watermark_rev_id."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(FETCH_PAGES_SQL, {"watermark": watermark_rev_id})
        for row in cur:
            wikitext = row["wikitext"]
            # Handle memoryview/bytes from PostgreSQL bytea columns
            if isinstance(wikitext, (bytes, memoryview)):
                wikitext = bytes(wikitext).decode("utf-8", errors="replace")
            yield WikiPage(
                page_id=row["page_id"],
                page_title=row["page_title"],
                page_ns=row["page_ns"],
                rev_id=row["rev_id"],
                wikitext=wikitext,
            )
