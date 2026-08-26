"""Vector stores for the workshop-manual index.

:class:`ChromaVectorStore` is the default when ``chromadb`` is installed;
:class:`InMemoryVectorStore` is a pure-Python fallback that persists to a JSON
file, so the RAG server works on a machine where Chroma will not build.

Both record which embedding backend produced the index. Searching an index
built with a different backend produces vectors of a different length -- or,
worse, the same length and meaningless numbers -- so the mismatch is detected
and reported rather than silently returning nonsense.
"""

from __future__ import annotations

import abc
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from majster_ai.errors import RagError
from majster_ai.logging_setup import get_logger
from majster_ai.mcp_servers.rag_workshop.embeddings import EmbeddingBackend, cosine_similarity

log = get_logger("mcp_servers.rag_workshop.store")


@dataclass(frozen=True, slots=True)
class Document:
    """One indexed chunk of a manual."""

    id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def source(self) -> str:
        return str(self.metadata.get("source", "unknown"))

    @property
    def page(self) -> int | None:
        page = self.metadata.get("page")
        return int(page) if isinstance(page, (int, float, str)) and str(page).isdigit() else None

    def citation(self) -> str:
        """A human-checkable reference, so claims can be traced to a page."""
        name = Path(self.source).name if self.source != "unknown" else "unknown source"
        return f"{name}, page {self.page}" if self.page is not None else name


@dataclass(frozen=True, slots=True)
class SearchResult:
    """A retrieved chunk and how well it matched."""

    document: Document
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.document.text,
            "score": round(self.score, 4),
            "source": self.document.source,
            "page": self.document.page,
            "citation": self.document.citation(),
            "metadata": self.document.metadata,
        }


class VectorStore(abc.ABC):
    """Storage and similarity search over embedded manual chunks."""

    #: Name of the embedding backend that built this index.
    embedding_name: str = "unknown"

    @abc.abstractmethod
    def add_documents(
        self, documents: Sequence[Document], vectors: Sequence[Sequence[float]]
    ) -> int:
        """Add documents with their pre-computed vectors. Returns the count added."""

    @abc.abstractmethod
    def search(self, vector: Sequence[float], top_k: int) -> list[SearchResult]:
        """Return the ``top_k`` closest documents, best first."""

    @abc.abstractmethod
    def count(self) -> int:
        """Number of indexed chunks."""

    @abc.abstractmethod
    def clear(self) -> None:
        """Drop everything -- used when re-ingesting from scratch."""

    def sources(self) -> list[str]:
        """Distinct source files in the index."""
        return []

    def describe(self) -> dict[str, Any]:
        return {
            "type": type(self).__name__,
            "documents": self.count(),
            "embedding_backend": self.embedding_name,
            "sources": self.sources(),
        }

    def check_compatible(self, backend: EmbeddingBackend) -> None:
        """Raise when the active backend did not build this index.

        Raises:
            RagError: naming the mismatch and the fix, because the alternative
                is a search that returns confident garbage.
        """
        if self.count() == 0 or self.embedding_name in ("unknown", ""):
            return
        if self.embedding_name != backend.name:
            raise RagError(
                f"This index was built with embedding backend "
                f"{self.embedding_name!r} but {backend.name!r} is active. "
                f"Vectors from different backends are not comparable. "
                f"Re-run ingestion to rebuild the index.",
                index_backend=self.embedding_name,
                active_backend=backend.name,
            )


class InMemoryVectorStore(VectorStore):
    """Pure-Python store with optional JSON persistence.

    Linear scan: fine for a workshop manual (thousands of chunks), and it
    removes every native dependency, which is what makes the RAG server
    runnable on a phone.
    """

    def __init__(self, path: str | Path | None = None, embedding_name: str = "unknown") -> None:
        self._path = Path(path) if path is not None else None
        self.embedding_name = embedding_name
        self._documents: list[Document] = []
        self._vectors: list[list[float]] = []
        if self._path is not None and self._path.is_file():
            self.load()

    def add_documents(
        self, documents: Sequence[Document], vectors: Sequence[Sequence[float]]
    ) -> int:
        if len(documents) != len(vectors):
            raise RagError(f"Document/vector count mismatch: {len(documents)} vs {len(vectors)}")
        known = {doc.id for doc in self._documents}
        added = 0
        for document, vector in zip(documents, vectors):
            if document.id in known:
                continue
            self._documents.append(document)
            self._vectors.append([float(value) for value in vector])
            known.add(document.id)
            added += 1
        return added

    def search(self, vector: Sequence[float], top_k: int) -> list[SearchResult]:
        if not self._documents:
            return []
        scored = [
            SearchResult(document=document, score=cosine_similarity(vector, candidate))
            for document, candidate in zip(self._documents, self._vectors)
        ]
        scored.sort(key=lambda result: result.score, reverse=True)
        return scored[: max(top_k, 0)]

    def count(self) -> int:
        return len(self._documents)

    def clear(self) -> None:
        self._documents.clear()
        self._vectors.clear()

    def sources(self) -> list[str]:
        return sorted({document.source for document in self._documents})

    def save(self) -> None:
        """Persist to JSON. No-op when no path was configured."""
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "embedding_backend": self.embedding_name,
            "documents": [asdict(document) for document in self._documents],
            "vectors": self._vectors,
        }
        # Write via a temporary file so an interrupted save cannot leave a
        # half-written index that fails to parse on the next run.
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(self._path)
        log.info("Saved %d chunk(s) to %s", len(self._documents), self._path)

    def load(self) -> None:
        """Load from JSON, tolerating a corrupt file by starting empty."""
        if self._path is None or not self._path.is_file():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Cannot read the index at %s (%s) - starting empty", self._path, exc)
            return
        self.embedding_name = payload.get("embedding_backend", "unknown")
        self._documents = [
            Document(id=d["id"], text=d["text"], metadata=d.get("metadata", {}))
            for d in payload.get("documents", [])
        ]
        self._vectors = [list(map(float, v)) for v in payload.get("vectors", [])]
        log.info("Loaded %d chunk(s) from %s", len(self._documents), self._path)


class ChromaVectorStore(VectorStore):
    """Persistent store backed by ChromaDB.

    Embeddings are supplied by us rather than by Chroma's own embedding
    function, so the same backend serves both stores and switching between
    them does not change retrieval behaviour.
    """

    def __init__(
        self,
        path: str | Path,
        collection_name: str = "workshop_manuals",
        embedding_name: str = "unknown",
    ) -> None:
        try:
            import chromadb
            from chromadb.config import Settings as ChromaSettings
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RagError(
                "chromadb is not installed. Install it with "
                "pip install 'car-diagnostic-ai[rag]', or use the in-memory store."
            ) from exc

        self._path = Path(path)
        self._path.mkdir(parents=True, exist_ok=True)
        self.embedding_name = embedding_name
        self._collection_name = collection_name

        try:
            self._client = chromadb.PersistentClient(
                path=str(self._path),
                settings=ChromaSettings(anonymized_telemetry=False, allow_reset=True),
            )
            self._collection = self._client.get_or_create_collection(
                name=collection_name,
                metadata={"embedding_backend": embedding_name},
            )
        except Exception as exc:
            raise RagError(f"Cannot open the Chroma index at {self._path}: {exc}") from exc

        # Trust the stored backend name over the one we were handed, so a
        # mismatch is detectable by check_compatible().
        stored = (self._collection.metadata or {}).get("embedding_backend")
        if stored:
            self.embedding_name = str(stored)

    def add_documents(
        self, documents: Sequence[Document], vectors: Sequence[Sequence[float]]
    ) -> int:
        if not documents:
            return 0
        if len(documents) != len(vectors):
            raise RagError(f"Document/vector count mismatch: {len(documents)} vs {len(vectors)}")
        try:
            self._collection.upsert(
                ids=[document.id for document in documents],
                embeddings=[list(map(float, vector)) for vector in vectors],
                documents=[document.text for document in documents],
                # Chroma metadata values must be scalars.
                metadatas=[
                    {
                        k: v
                        for k, v in document.metadata.items()
                        if isinstance(v, (str, int, float, bool))
                    }
                    or {"source": document.source}
                    for document in documents
                ],
            )
        except Exception as exc:
            raise RagError(f"Chroma rejected the documents: {exc}") from exc
        return len(documents)

    def search(self, vector: Sequence[float], top_k: int) -> list[SearchResult]:
        if self.count() == 0:
            return []
        try:
            response = self._collection.query(
                query_embeddings=[list(map(float, vector))],
                n_results=max(min(top_k, self.count()), 1),
                include=["documents", "metadatas", "distances"],
            )
        except Exception as exc:
            raise RagError(f"Chroma query failed: {exc}") from exc

        ids = (response.get("ids") or [[]])[0]
        texts = (response.get("documents") or [[]])[0]
        metadatas = (response.get("metadatas") or [[]])[0]
        distances = (response.get("distances") or [[]])[0]

        results: list[SearchResult] = []
        for index, identifier in enumerate(ids):
            distance = float(distances[index]) if index < len(distances) else 1.0
            # Chroma returns squared L2 distance by default; on normalised
            # vectors that maps to cosine similarity as 1 - d/2.
            results.append(
                SearchResult(
                    document=Document(
                        id=str(identifier),
                        text=texts[index] if index < len(texts) else "",
                        metadata=(
                            dict(metadatas[index])
                            if index < len(metadatas) and metadatas[index]
                            else {}
                        ),
                    ),
                    score=max(0.0, min(1.0, 1.0 - distance / 2.0)),
                )
            )
        return results

    def count(self) -> int:
        try:
            return int(self._collection.count())
        except Exception:  # pragma: no cover - defensive
            return 0

    def clear(self) -> None:
        try:
            self._client.delete_collection(self._collection_name)
            self._collection = self._client.get_or_create_collection(
                name=self._collection_name,
                metadata={"embedding_backend": self.embedding_name},
            )
        except Exception as exc:
            raise RagError(f"Cannot clear the Chroma collection: {exc}") from exc

    def sources(self) -> list[str]:
        try:
            payload = self._collection.get(include=["metadatas"])
        except Exception:  # pragma: no cover - defensive
            return []
        found = {
            str(metadata.get("source"))
            for metadata in (payload.get("metadatas") or [])
            if metadata and metadata.get("source")
        }
        return sorted(found)

    def save(self) -> None:
        """Chroma's PersistentClient writes through; nothing to do."""


def build_store(
    vector_dir: str | Path,
    collection_name: str,
    embedding_name: str,
    *,
    prefer_chroma: bool = True,
) -> VectorStore:
    """Pick the best store available, degrading to in-memory with a warning."""
    if prefer_chroma:
        try:
            return ChromaVectorStore(vector_dir, collection_name, embedding_name)
        except RagError as exc:
            log.warning("Falling back to the in-memory store: %s", exc.message)
    path = Path(vector_dir) / f"{collection_name}.json"
    return InMemoryVectorStore(path, embedding_name=embedding_name)


def iter_documents(documents: Iterable[Document]) -> list[Document]:
    """Materialise an iterable of documents, dropping empty chunks."""
    return [document for document in documents if document.text.strip()]


__all__ = [
    "Document",
    "SearchResult",
    "VectorStore",
    "InMemoryVectorStore",
    "ChromaVectorStore",
    "build_store",
    "iter_documents",
]
