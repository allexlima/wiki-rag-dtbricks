"""
Cleans MediaWiki wikitext into plain text using mwparserfromhell.
Also extracts image references before stripping markup.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import mwparserfromhell

# MW image option keywords to strip from alt text
_IMAGE_OPTIONS = re.compile(
    r"^(thumb|thumbnail|frame|frameless|border|left|right|center|none"
    r"|upright|baseline|sub|super|top|text-top|middle|bottom|text-bottom"
    r"|\d+px|\d+x\d+px)$",
    re.IGNORECASE,
)


@dataclass
class ImageRef:
    """An image reference extracted from wikitext."""
    filename: str
    alt_text: str


def extract_image_refs(wikitext: str) -> list[ImageRef]:
    """Extract [[File:...]] and [[Image:...]] references from wikitext.

    Must be called BEFORE clean_wikitext(), which strips these references.
    """
    if not wikitext or not wikitext.strip():
        return []

    wikicode = mwparserfromhell.parse(wikitext)
    refs: list[ImageRef] = []

    for link in wikicode.filter_wikilinks():
        title = str(link.title).strip()
        if not re.match(r"^(File|Image):", title, re.IGNORECASE):
            continue

        # Extract filename after "File:" or "Image:" prefix
        filename = re.sub(r"^(File|Image):", "", title, flags=re.IGNORECASE).strip()
        if not filename:
            continue

        # Extract alt text — filter out MW option keywords
        alt_text = ""
        if link.text:
            parts = str(link.text).split("|")
            text_parts = [p.strip() for p in parts if not _IMAGE_OPTIONS.match(p.strip())]
            alt_text = " ".join(text_parts).strip()

        refs.append(ImageRef(filename=filename, alt_text=alt_text))

    return refs

_SKIP_TEMPLATES = frozenset([
    "stub", "citation needed", "reflist", "references",
    "infobox", "navbox", "hatnote", "short description",
])


def clean_wikitext(wikitext: str) -> str:
    """Parse wikitext and return clean plain text for chunking."""
    if not wikitext or not wikitext.strip():
        return ""

    wikicode = mwparserfromhell.parse(wikitext)

    # Remove noisy templates
    for template in wikicode.filter_templates():
        name = template.name.strip().lower()
        if name in _SKIP_TEMPLATES:
            try:
                wikicode.remove(template)
            except ValueError:
                pass

    # Strip all wiki markup to plain text
    text = wikicode.strip_code(normalize=True, collapse=True, keep_template_params=False)

    # Collapse excessive blank lines
    lines = text.splitlines()
    clean_lines: list[str] = []
    prev_blank = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if not prev_blank:
                clean_lines.append("")
            prev_blank = True
        else:
            prev_blank = False
            clean_lines.append(stripped)

    return "\n".join(clean_lines).strip()
