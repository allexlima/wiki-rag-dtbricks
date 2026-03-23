"""Wiki pipeline — clean, chunk, embed, and caption wiki content."""
from __future__ import annotations

import base64
import io
import logging
import os
import re
from dataclasses import dataclass

import mwparserfromhell
import requests
from databricks_langchain import ChatDatabricks, DatabricksEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)


@dataclass
class ImageRef:
    filename: str
    alt_text: str


@dataclass
class TextChunk:
    page_id: int
    page_title: str
    page_ns: int
    rev_id: int
    chunk_index: int
    text: str
    chunk_source: str = "text"


_IMAGE_OPTIONS = re.compile(
    r"^(thumb|thumbnail|frame|frameless|border|left|right|center|none"
    r"|upright|baseline|sub|super|top|text-top|middle|bottom|text-bottom"
    r"|\d+px|\d+x\d+px)$",
    re.IGNORECASE,
)

_SKIP_TEMPLATES = frozenset([
    "stub", "citation needed", "reflist", "references",
    "infobox", "navbox", "hatnote", "short description",
])

_splitter = RecursiveCharacterTextSplitter(
    chunk_size=512,
    chunk_overlap=64,
    separators=["\n\n", "\n", ". ", " ", ""],
    length_function=len,
)


def _resize_image(image_bytes: bytes, max_dim: int = 1024) -> bytes:
    """Resize image to fit within *max_dim* × *max_dim*, preserving aspect ratio."""
    from PIL import Image

    img = Image.open(io.BytesIO(image_bytes))
    if max(img.size) <= max_dim:
        return image_bytes
    img.thumbnail((max_dim, max_dim), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format=img.format or "PNG")
    return buf.getvalue()


def _guess_mime(filename: str) -> str:
    """Guess MIME type from filename extension."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "png"
    return {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "gif": "image/gif",
        "svg": "image/svg+xml", "webp": "image/webp",
    }.get(ext, "image/png")


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=30))
def _embed_batch(embeddings_model: DatabricksEmbeddings, batch: list[str]) -> list[list[float]]:
    """Embed a single batch with retry on transient failures."""
    return embeddings_model.embed_documents(batch)


class WikiPipeline:
    """End-to-end processing pipeline for wiki content."""

    CHUNK_SIZE: int = 512
    CHUNK_OVERLAP: int = 64
    EMBEDDING_MODEL: str = os.environ.get("EMBEDDING_MODEL", "databricks-qwen3-embedding-0-6b")
    EMBEDDING_DIMS: int = 1024
    EMBEDDING_BATCH_SIZE: int = 64
    VISION_MODEL: str = os.environ.get("VISION_MODEL", "databricks-claude-sonnet-4-6")
    MEDIAWIKI_URL: str = "http://localhost:8080"

    @staticmethod
    def extract_image_refs(wikitext: str) -> list[ImageRef]:
        """Extract [[File:...]] and [[Image:...]] references from wikitext."""
        if not wikitext or not wikitext.strip():
            return []

        wikicode = mwparserfromhell.parse(wikitext)
        refs: list[ImageRef] = []

        for link in wikicode.filter_wikilinks():
            title = str(link.title).strip()
            if not re.match(r"^(File|Image):", title, re.IGNORECASE):
                continue

            filename = re.sub(r"^(File|Image):", "", title, flags=re.IGNORECASE).strip()
            if not filename:
                continue

            alt_text = ""
            if link.text:
                parts = str(link.text).split("|")
                text_parts = [p.strip() for p in parts if not _IMAGE_OPTIONS.match(p.strip())]
                alt_text = " ".join(text_parts).strip()

            refs.append(ImageRef(filename=filename, alt_text=alt_text))

        return refs

    @staticmethod
    def clean_wikitext(wikitext: str) -> str:
        """Strip wikitext markup to clean plain text for chunking."""
        if not wikitext or not wikitext.strip():
            return ""

        wikicode = mwparserfromhell.parse(wikitext)

        for template in wikicode.filter_templates():
            name = template.name.strip().lower()
            if name in _SKIP_TEMPLATES:
                try:
                    wikicode.remove(template)
                except ValueError:
                    pass

        text = wikicode.strip_code(normalize=True, collapse=True, keep_template_params=False)

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

    @staticmethod
    def chunk_page(
        page_id: int,
        page_title: str,
        page_ns: int,
        rev_id: int,
        clean_text: str,
    ) -> list[TextChunk]:
        """Split cleaned text into overlapping TextChunks."""
        if not clean_text.strip():
            return []

        raw_chunks = _splitter.split_text(clean_text)
        return [
            TextChunk(
                page_id=page_id,
                page_title=page_title,
                page_ns=page_ns,
                rev_id=rev_id,
                chunk_index=i,
                text=chunk,
            )
            for i, chunk in enumerate(raw_chunks)
        ]

    @staticmethod
    def chunk_image_caption(
        page_id: int,
        page_title: str,
        page_ns: int,
        rev_id: int,
        filename: str,
        caption: str,
        chunk_index_offset: int = 0,
    ) -> list[TextChunk]:
        """Create image-sourced chunks from a vision LLM caption."""
        text = f'[Image from "{page_title}": {filename}]\n{caption}'
        if not text.strip():
            return []

        raw_chunks = _splitter.split_text(text)
        return [
            TextChunk(
                page_id=page_id,
                page_title=page_title,
                page_ns=page_ns,
                rev_id=rev_id,
                chunk_index=chunk_index_offset + i,
                text=chunk,
                chunk_source="image",
            )
            for i, chunk in enumerate(raw_chunks)
        ]

    @classmethod
    def embed_texts(cls, texts: list[str], model: str | None = None) -> list[list[float]]:
        """Embed texts in batches via Foundation Model API."""
        model = model or cls.EMBEDDING_MODEL
        embeddings_model = DatabricksEmbeddings(endpoint=model)
        all_embeddings: list[list[float]] = []

        for i in range(0, len(texts), cls.EMBEDDING_BATCH_SIZE):
            batch = texts[i : i + cls.EMBEDDING_BATCH_SIZE]
            log.debug("Embedding batch %d–%d of %d texts", i, i + len(batch), len(texts))
            embeddings = _embed_batch(embeddings_model, batch)
            all_embeddings.extend(embeddings)

        return all_embeddings

    @classmethod
    def fetch_image_from_mediawiki(
        cls,
        filename: str,
        base_url: str | None = None,
        timeout: int = 30,
    ) -> bytes | None:
        """Fetch image bytes from MediaWiki's imageinfo API. Returns None on failure."""
        base_url = base_url or cls.MEDIAWIKI_URL
        try:
            resp = requests.get(
                f"{base_url}/api.php",
                params={
                    "action": "query",
                    "titles": f"File:{filename}",
                    "prop": "imageinfo",
                    "iiprop": "url",
                    "format": "json",
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()

            pages = data.get("query", {}).get("pages", {})
            for page in pages.values():
                imageinfo = page.get("imageinfo", [])
                if imageinfo:
                    url = imageinfo[0].get("url", "")
                    if url:
                        img_resp = requests.get(url, timeout=timeout)
                        img_resp.raise_for_status()
                        log.info("Fetched image '%s' (%d bytes)", filename, len(img_resp.content))
                        return img_resp.content

            log.warning("No image URL found for '%s'", filename)
            return None

        except Exception:
            log.warning("Failed to fetch image '%s'", filename, exc_info=True)
            return None

    @classmethod
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=30))
    def caption_image(
        cls,
        image_bytes: bytes,
        alt_text: str = "",
        page_title: str = "",
        filename: str = "",
        model: str | None = None,
    ) -> str:
        """Generate a text description of an image using a vision LLM."""
        model = model or cls.VISION_MODEL
        resized = _resize_image(image_bytes)
        b64_data = base64.b64encode(resized).decode("utf-8")
        mime = _guess_mime(filename or page_title)

        vision_llm = ChatDatabricks(endpoint=model)

        context_hint = f' This image appears on the wiki page "{page_title}".' if page_title else ""
        alt_hint = f" The image alt text is: {alt_text}." if alt_text else ""

        from langchain_core.messages import HumanMessage, SystemMessage

        response = vision_llm.invoke([
                SystemMessage(content=(
                        "You are an image description assistant for a wiki knowledge base. "
                        "Produce a factual, information-dense description (2-4 sentences) "
                        "that captures key visual details: diagrams, charts, organizational "
                        "structures, labels, text overlays, and relationships shown."
                    )),
                HumanMessage(content=[
                        {
                            "type": "text",
                            "text": f"Describe this image in detail.{context_hint}{alt_hint}",
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64_data}"},
                        },
                    ]),
            ],
            max_tokens=256,
            temperature=0,
        )
        caption = response.content
        if not caption:
            return f"[Image: {alt_text}]" if alt_text else "[Image]"
        return caption.strip()
