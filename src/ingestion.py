"""
MediaWiki ingestion — reads wikitext from native PostgreSQL tables on Lakebase.

Table relationships (MediaWiki 1.42 Multi-Content Revisions):
    page.page_latest → revision.rev_id
    → slots.slot_revision_id → content.content_id
    → SUBSTRING(content_address, 4) → text.old_id
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Generator

import psycopg2
from psycopg2.extras import RealDictCursor


@dataclass
class WikiPage:
    """A wiki page with its current wikitext content."""

    page_id: int
    page_title: str
    page_ns: int
    rev_id: int
    wikitext: str


_FETCH_PAGES_SQL = """
SELECT
    p.page_id,
    REPLACE(p.page_title, '_', ' ') AS page_title,
    p.page_namespace AS page_ns,
    r.rev_id,
    t.old_text AS wikitext
FROM mediawiki.page p
JOIN mediawiki.revision r
    ON r.rev_id = p.page_latest
JOIN mediawiki.slots s
    ON s.slot_revision_id = r.rev_id
JOIN mediawiki.content c
    ON c.content_id = s.slot_content_id
JOIN mediawiki."text" t
    ON SUBSTRING(c.content_address FROM 4)::BIGINT = t.old_id
WHERE p.page_namespace = 0
  AND r.rev_id > %(watermark)s
ORDER BY r.rev_id ASC;
"""


class MediaWikiIngestion:
    """Reads current wikitext from MediaWiki's native PostgreSQL tables.

    MediaWiki stores page content across several normalized tables using
    the Multi-Content Revisions (MCR) schema introduced in MW 1.32+.
    This class joins through the chain to extract the latest wikitext
    for each main-namespace page.
    """

    def fetch_pages(
        self,
        conn: psycopg2.extensions.connection,
        watermark_rev_id: int = 0,
    ) -> Generator[WikiPage, None, None]:
        """Yield WikiPage objects for all pages updated after *watermark_rev_id*.

        Uses a server-side cursor to stream results without loading
        the entire result set into memory.

        Args:
            conn: A psycopg2 connection to the Lakebase database.
            watermark_rev_id: Only return pages whose ``rev_id`` is greater
                than this value.  Defaults to ``0`` (all pages).

        Yields:
            WikiPage: One object per page, in ``rev_id`` ascending order.
        """
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(_FETCH_PAGES_SQL, {"watermark": watermark_rev_id})
            for row in cur:
                wikitext = row["wikitext"]
                if isinstance(wikitext, (bytes, memoryview)):
                    wikitext = bytes(wikitext).decode("utf-8", errors="replace")
                yield WikiPage(
                    page_id=row["page_id"],
                    page_title=row["page_title"],
                    page_ns=row["page_ns"],
                    rev_id=row["rev_id"],
                    wikitext=wikitext,
                )
