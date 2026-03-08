"""
Chunks cleaned wiki text using LangChain's RecursiveCharacterTextSplitter.
"""
from __future__ import annotations

from dataclasses import dataclass

from langchain_text_splitters import RecursiveCharacterTextSplitter

CHUNK_SIZE = 512
CHUNK_OVERLAP = 64

_splitter = RecursiveCharacterTextSplitter(
    chunk_size=CHUNK_SIZE,
    chunk_overlap=CHUNK_OVERLAP,
    separators=["\n\n", "\n", ". ", " ", ""],
    length_function=len,
)


@dataclass
class TextChunk:
    page_id: int
    page_title: str
    page_ns: int
    rev_id: int
    chunk_index: int
    text: str
    chunk_source: str = "text"  # "text" or "image"


def chunk_page(
    page_id: int,
    page_title: str,
    page_ns: int,
    rev_id: int,
    clean_text: str,
) -> list[TextChunk]:
    """Split a cleaned wiki page into overlapping text chunks."""
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


def chunk_image_caption(
    page_id: int,
    page_title: str,
    page_ns: int,
    rev_id: int,
    filename: str,
    caption: str,
    chunk_index_offset: int = 0,
) -> list[TextChunk]:
    """Create chunks from an image caption (tagged as image-sourced)."""
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
