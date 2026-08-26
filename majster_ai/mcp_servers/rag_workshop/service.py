"""Workshop-manual retrieval.

Ties the embedding backend, the vector store and the ingestion pipeline
together behind two operations: build the index, and search it.

Every result carries a citation (file name and page). That is deliberate: the
agent is going to paraphrase these passages into repair advice, and a human
under a car needs to be able to check the claim against the actual manual
before undoing a suspension bolt.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from majster_ai.config import Settings, get_settings
from majster_ai.errors import IndexNotBuiltError, MajsterError, RagError
from majster_ai.logging_setup import get_logger, log_agent_step
from majster_ai.mcp_servers.rag_workshop.embeddings import EmbeddingBackend, build_embeddings
from majster_ai.mcp_servers.rag_workshop.ingest import (
    SUPPORTED_EXTENSIONS,
    discover_manuals,
    documents_from_directory,
)
from majster_ai.mcp_servers.rag_workshop.store import Document, VectorStore, build_store

log = get_logger("mcp_servers.rag_workshop.service")

#: Below this cosine similarity a "match" is almost certainly noise. Returning
#: it anyway invites the model to build advice on an unrelated passage.
MIN_RELEVANCE_SCORE = 0.05

#: Chunks are embedded in batches to bound peak memory on small devices.
INGEST_BATCH_SIZE = 64


class RagService:
    """Index and search local workshop manuals."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        embeddings: EmbeddingBackend | None = None,
        store: VectorStore | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.embeddings = embeddings or build_embeddings(self.settings.rag_embedding_model)
        self.store = store or build_store(
            self.settings.vector_dir,
            self.settings.rag_collection,
            self.embeddings.name,
        )

    # -- ingestion ----------------------------------------------------------
    def ingest(
        self, *, rebuild: bool = False, manuals_dir: str | Path | None = None
    ) -> dict[str, Any]:
        """Load, chunk, embed and index every manual in the manuals directory.

        Args:
            rebuild: Drop the existing index first. Required when switching
                embedding backends, since vectors are not comparable across
                them.
            manuals_dir: Override the configured directory.

        Returns:
            Counts and timings, or a structured error.
        """
        directory = Path(manuals_dir or self.settings.manuals_dir)
        started = time.monotonic()

        if not directory.is_dir():
            return {
                "ok": False,
                "error": "manuals_dir_missing",
                "message": (
                    f"The manuals directory {directory} does not exist. Create it "
                    f"and put your workshop manual PDFs in it."
                ),
            }

        files = discover_manuals(directory)
        if not files:
            return {
                "ok": False,
                "error": "no_manuals_found",
                "message": (
                    f"No readable documents in {directory}. Supported extensions: "
                    f"{', '.join(sorted(SUPPORTED_EXTENSIONS))}."
                ),
                "manuals_dir": str(directory),
            }

        if rebuild:
            log.info("Rebuilding the index from scratch")
            self.store.clear()
            self.store.embedding_name = self.embeddings.name
        else:
            try:
                self.store.check_compatible(self.embeddings)
            except RagError as exc:
                payload = exc.to_dict()
                payload["hint"] = "Re-run ingestion with rebuild=true."
                return payload

        log_agent_step("rag.ingest", f"Indexing {len(files)} document(s) from {directory}")

        total_chunks = 0
        added = 0
        batch: list[Document] = []

        def flush() -> int:
            nonlocal batch
            if not batch:
                return 0
            vectors = self.embeddings.embed_documents([doc.text for doc in batch])
            count = self.store.add_documents(batch, vectors)
            batch = []
            return count

        try:
            for document in documents_from_directory(
                directory,
                chunk_size=self.settings.rag_chunk_size,
                chunk_overlap=self.settings.rag_chunk_overlap,
                files=files,
            ):
                batch.append(document)
                total_chunks += 1
                if len(batch) >= INGEST_BATCH_SIZE:
                    added += flush()
            added += flush()
        except MajsterError as exc:
            return exc.to_dict()

        if hasattr(self.store, "save"):
            self.store.save()

        elapsed = time.monotonic() - started
        return {
            "ok": True,
            "manuals_dir": str(directory),
            "files_indexed": [path.name for path in files],
            "file_count": len(files),
            "chunks_processed": total_chunks,
            "chunks_added": added,
            "total_in_index": self.store.count(),
            "embedding_backend": self.embeddings.describe(),
            "elapsed_seconds": round(elapsed, 2),
            "summary": (
                f"Indexed {total_chunks} chunk(s) from {len(files)} file(s) in "
                f"{elapsed:.1f}s. The index now holds {self.store.count()} chunk(s)."
            ),
        }

    # -- retrieval ----------------------------------------------------------
    def search_manual(
        self,
        query: str,
        top_k: int | None = None,
        source_filter: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve the passages most relevant to ``query``.

        Args:
            query: What to look for. Works best with the vocabulary the manual
                itself uses.
            top_k: Number of passages. Defaults to the configured value.
            source_filter: Case-insensitive substring of the file name, to
                restrict the search to one manual.

        Returns:
            Ranked passages with citations, or a structured error.
        """
        if not query or not query.strip():
            return {
                "ok": False,
                "error": "empty_query",
                "message": "Provide a search query, e.g. 'turbocharger actuator adjustment'.",
            }

        try:
            if self.store.count() == 0:
                raise IndexNotBuiltError(
                    "The workshop-manual index is empty. Put your manual PDFs in "
                    f"{self.settings.manuals_dir} and run 'majster-ai ingest' "
                    f"(or call the ingest_manuals tool).",
                    manuals_dir=str(self.settings.manuals_dir),
                )
            self.store.check_compatible(self.embeddings)
        except MajsterError as exc:
            return exc.to_dict()

        limit = top_k if top_k and top_k > 0 else self.settings.rag_top_k
        log_agent_step("rag.search", f"Searching the manuals for: {query!r}", top_k=limit)

        try:
            vector = self.embeddings.embed_query(query)
            # Over-fetch when filtering so the filter does not starve the result.
            fetched = self.store.search(vector, limit * 4 if source_filter else limit)
        except MajsterError as exc:
            return exc.to_dict()
        except Exception as exc:
            return RagError(f"Manual search failed: {exc}").to_dict()

        results = [result for result in fetched if result.score >= MIN_RELEVANCE_SCORE]
        if source_filter:
            needle = source_filter.lower()
            results = [
                result for result in results if needle in Path(result.document.source).name.lower()
            ]
        results = results[:limit]

        payload: dict[str, Any] = {
            "ok": True,
            "query": query,
            "count": len(results),
            "results": [result.to_dict() for result in results],
            "embedding_backend": self.embeddings.describe(),
            "index_size": self.store.count(),
        }

        if not self.embeddings.semantic:
            payload["retrieval_caveat"] = (
                "This index uses lexical hash embeddings, not semantic ones. It "
                "matches shared wording rather than meaning, so a passage phrased "
                "differently from the query may be missed. Install "
                "'car-diagnostic-ai[rag-local-embeddings]' for semantic search."
            )

        if not results:
            payload["summary"] = (
                f"Nothing in the indexed manuals matched {query!r}. Try the "
                f"manual's own wording (a DTC number, a component name), or "
                f"search the web instead."
            )
        else:
            best = results[0]
            payload["summary"] = (
                f"{len(results)} passage(s) found. Best match: {best.document.citation()} "
                f"(score {best.score:.2f})."
            )
            payload["citations"] = [result.document.citation() for result in results]
        return payload

    # -- introspection ------------------------------------------------------
    def status(self) -> dict[str, Any]:
        directory = Path(self.settings.manuals_dir)
        files = discover_manuals(directory) if directory.is_dir() else []
        return {
            "ok": True,
            "index": self.store.describe(),
            "embedding_backend": self.embeddings.describe(),
            "manuals_dir": str(directory),
            "manuals_dir_exists": directory.is_dir(),
            "documents_available": [path.name for path in files],
            "indexed": self.store.count(),
            "ready": self.store.count() > 0,
            "note": (
                "Run ingest_manuals to (re)build the index after adding files."
                if self.store.count() == 0
                else "Index ready."
            ),
        }

    def list_sources(self) -> dict[str, Any]:
        sources = self.store.sources()
        return {
            "ok": True,
            "count": len(sources),
            "sources": [Path(source).name for source in sources],
            "paths": sources,
        }


__all__ = ["RagService", "MIN_RELEVANCE_SCORE", "INGEST_BATCH_SIZE"]
