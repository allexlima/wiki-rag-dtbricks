"""
Cleans MediaWiki wikitext into plain text using mwparserfromhell.
"""
import mwparserfromhell

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
        if any(skip in name for skip in _SKIP_TEMPLATES):
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
