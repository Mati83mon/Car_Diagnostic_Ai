"""RAG_Workshop_MCP -- local retrieval over workshop manuals.

Runs entirely on-device: copyrighted service documentation is never uploaded
anywhere. Degrades gracefully from semantic embeddings + ChromaDB down to a
lexical hash vectoriser + a JSON index, so it still works on a phone.
"""

from __future__ import annotations

from majster_ai.mcp_servers.rag_workshop.embeddings import (
    EmbeddingBackend,
    HashEmbeddings,
    SentenceTransformerEmbeddings,
    build_embeddings,
)
from majster_ai.mcp_servers.rag_workshop.service import RagService
from majster_ai.mcp_servers.rag_workshop.store import Document, SearchResult, build_store

__all__ = [
    "RagService",
    "EmbeddingBackend",
    "HashEmbeddings",
    "SentenceTransformerEmbeddings",
    "build_embeddings",
    "Document",
    "SearchResult",
    "build_store",
]
