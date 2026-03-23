"""MediaWiki ingestion — reads wikitext from native PostgreSQL tables."""
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
    """Reads current wikitext from MediaWiki's native PostgreSQL tables."""

    def fetch_pages(
        self,
        conn: psycopg2.extensions.connection,
        watermark_rev_id: int = 0,
    ) -> Generator[WikiPage, None, None]:
        """Yield WikiPage objects for pages updated after *watermark_rev_id*."""
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
