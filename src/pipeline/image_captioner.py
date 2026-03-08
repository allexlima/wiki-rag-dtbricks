"""
Fetches images from MediaWiki and generates text descriptions using a vision LLM.

Pipeline-time captioning: images are described once during ingestion,
and the captions are stored as regular text chunks for embedding.
"""
from __future__ import annotations

import base64
import io
import logging
import os
from urllib.parse import quote

import requests
from databricks_openai import DatabricksOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)

VISION_MODEL = os.environ.get("VISION_MODEL", "databricks-claude-sonnet-4-6")
MEDIAWIKI_URL = os.environ.get("MEDIAWIKI_URL", "http://localhost:8080")
MAX_IMAGE_DIMENSION = 1024  # px — resize larger images before sending to vision API


def _resize_image(image_bytes: bytes, max_dim: int = MAX_IMAGE_DIMENSION) -> bytes:
    """Resize image to fit within max_dim x max_dim, preserving aspect ratio."""
    from PIL import Image

    img = Image.open(io.BytesIO(image_bytes))

    if max(img.size) <= max_dim:
        return image_bytes

    img.thumbnail((max_dim, max_dim), Image.LANCZOS)
    buf = io.BytesIO()
    fmt = img.format or "PNG"
    img.save(buf, format=fmt)
    return buf.getvalue()


def _guess_mime(filename: str) -> str:
    """Guess MIME type from filename extension."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "png"
    return {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
        "svg": "image/svg+xml",
        "webp": "image/webp",
    }.get(ext, "image/png")


def fetch_image_from_mediawiki(
    filename: str,
    base_url: str = MEDIAWIKI_URL,
    timeout: int = 30,
) -> bytes | None:
    """Fetch image bytes from MediaWiki via its API.

    Uses the imageinfo API to resolve the actual image URL,
    then downloads the file.
    """
    try:
        # Query MW API for image URL
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


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=30))
def caption_image(
    image_bytes: bytes,
    alt_text: str = "",
    page_title: str = "",
    model: str = VISION_MODEL,
) -> str:
    """Generate a text description of an image using a vision-capable LLM.

    Returns a 2-4 sentence description suitable for embedding and retrieval.
    Falls back to alt_text if the vision call fails after retries.
    """
    resized = _resize_image(image_bytes)
    b64_data = base64.b64encode(resized).decode("utf-8")
    mime = _guess_mime(page_title)  # best-effort, fallback to png

    client = DatabricksOpenAI()

    context_hint = f' This image appears on the wiki page "{page_title}".' if page_title else ""
    alt_hint = f" The image alt text is: {alt_text}." if alt_text else ""

    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an image description assistant for a wiki knowledge base. "
                    "Produce a factual, information-dense description (2-4 sentences) "
                    "that captures key visual details: diagrams, charts, organizational "
                    "structures, labels, text overlays, and relationships shown."
                ),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"Describe this image in detail.{context_hint}{alt_hint}",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64_data}"},
                    },
                ],
            },
        ],
        max_tokens=256,
        temperature=0,
        timeout=60,
    )
    caption = response.choices[0].message.content
    if not caption:
        return f"[Image: {alt_text}]" if alt_text else "[Image]"
    return caption.strip()
