"""Metadata-preserving document chunking with character and paragraph strategies."""
from __future__ import annotations

import os
import re

from .document import Chunk, Document


def _chunk(text: str, tenant_id: str, department: str, document_id: str,
           version: str, permission: str, created_at, start: int, index: int) -> Chunk:
    return Chunk(
        text=text, tenant_id=tenant_id, department=department,
        document_id=document_id, version=version, permission=permission,
        created_at=created_at, chunk_id=index,
    )


def _character_chunks(doc: Document, max_chars: int, overlap: int) -> list[Chunk]:
    text = doc.text or ""
    chunks: list[Chunk] = []
    step = max(1, max_chars - overlap)
    index = 0
    for start in range(0, max(1, len(text)), step):
        piece = text[start:start + max_chars]
        if not piece:
            break
        chunks.append(_chunk(
            piece, doc.tenant_id, doc.department, doc.document_id, doc.version,
            doc.permission, doc.created_at, start, index,
        ))
        index += 1
        if start + max_chars >= len(text):
            break
    return chunks


def _paragraph_chunks(doc: Document, max_chars: int) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", doc.text or "") if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            temporary = Document(
                text=paragraph, tenant_id=doc.tenant_id, department=doc.department,
                document_id=doc.document_id, version=doc.version,
                permission=doc.permission, created_at=doc.created_at,
            )
            chunks.extend(piece.text for piece in _character_chunks(temporary, max_chars, 0))
            continue
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) > max_chars:
            chunks.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def ingest(doc: Document, max_chars: int = 500, overlap: int = 50,
           strategy: str | None = None) -> list[Chunk]:
    if max_chars <= 0:
        raise ValueError("max_chars must be > 0")
    if overlap < 0 or overlap >= max_chars:
        raise ValueError("overlap must be between 0 and max_chars")
    strategy = strategy or os.getenv("RAG_CHUNK_STRATEGY", "character")
    if strategy == "paragraph":
        chunks = [
            _chunk(text, doc.tenant_id, doc.department, doc.document_id, doc.version,
                   doc.permission, doc.created_at, 0, index)
            for index, text in enumerate(_paragraph_chunks(doc, max_chars))
        ]
    else:
        chunks = _character_chunks(doc, max_chars, overlap)
    return chunks or [_chunk(
        "", doc.tenant_id, doc.department, doc.document_id, doc.version,
        doc.permission, doc.created_at, 0, 0,
    )]
