"""Embedding backends for the workshop-manual index.

Two backends, chosen at runtime:

:class:`SentenceTransformerEmbeddings`
    Real semantic embeddings (``all-MiniLM-L6-v2`` by default). Runs locally,
    so copyrighted manuals never leave the machine. Preferred.

:class:`HashEmbeddings`
    A dependency-free hashing vectoriser. Used when ``sentence-transformers``
    cannot be installed -- which on a phone running Termux is the normal case,
    not an edge case.

Be clear about what the fallback is: it is **lexical**, not semantic. It will
find "diesel particulate filter" when you search for "DPF regeneration"
only because they share tokens, and it will not connect "won't boost" to
"turbocharger underboost" at all. That is a real quality difference, so the
service reports which backend is active and the tool output says so too. A
retrieval system that quietly degrades is worse than one that admits it.
"""

from __future__ import annotations

import abc
import hashlib
import math
import re
from typing import Any, Final, Sequence

from majster_ai.logging_setup import get_logger

log = get_logger("mcp_servers.rag_workshop.embeddings")

#: Dimensionality of the hash fallback. 512 keeps collisions tolerable for
#: manual-sized corpora while staying cheap on a phone.
HASH_DIMENSIONS: Final = 512

_TOKEN_RE: Final = re.compile(r"[a-z0-9]+(?:[-_/][a-z0-9]+)*")

#: Words carrying no retrieval signal in an automotive corpus.
_STOPWORDS: Final[frozenset[str]] = frozenset("""
    a an and are as at be been but by can could do does for from had has have
    how i if in into is it its may might must of on or should so than that the
    their then there these they this those to was were what when where which
    who will with would you your
    """.split())


class EmbeddingBackend(abc.ABC):
    """Turns text into vectors."""

    #: Vector length. Fixed for the lifetime of an index.
    dimensions: int = 0
    #: Short identifier stored alongside the index so a mismatch is detectable.
    name: str = "unknown"
    #: True when the vectors carry semantic meaning rather than lexical overlap.
    semantic: bool = False

    @abc.abstractmethod
    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed a batch of documents."""

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query. Defaults to the document path."""
        return self.embed_documents([text])[0]

    def describe(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "dimensions": self.dimensions,
            "semantic": self.semantic,
        }


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, stopwords removed.

    Hyphenated and slashed automotive terms are kept whole: ``iso-tp``,
    ``p0299``, ``egr/dpf`` are single meaningful tokens, and splitting them
    would destroy exactly the signal we need.
    """
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS and len(t) > 1]


class HashEmbeddings(EmbeddingBackend):
    """A signed hashing vectoriser -- lexical retrieval with no dependencies.

    Unigrams and bigrams are hashed into a fixed-width vector with a signed
    hash (so collisions cancel rather than accumulate), weighted by sub-linear
    term frequency, and L2-normalised so that a dot product is a cosine
    similarity.
    """

    semantic = False

    def __init__(self, dimensions: int = HASH_DIMENSIONS) -> None:
        if dimensions < 16:
            raise ValueError("HashEmbeddings needs at least 16 dimensions")
        self.dimensions = dimensions
        self.name = f"hash-{dimensions}"

    @staticmethod
    def _hash(token: str) -> tuple[int, float]:
        """Map a token to (bucket, sign) deterministically across processes.

        ``hashlib`` rather than ``hash()``: Python randomises string hashing
        per process, which would silently invalidate a persisted index on
        every restart.
        """
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        return value >> 1, 1.0 if value & 1 else -1.0

    def _embed_one(self, text: str) -> list[float]:
        tokens = tokenize(text)
        if not tokens:
            return [0.0] * self.dimensions

        counts: dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1
        # Bigrams capture short phrases: "swirl flap", "rail pressure".
        for first, second in zip(tokens, tokens[1:]):
            bigram = f"{first}_{second}"
            counts[bigram] = counts.get(bigram, 0) + 1

        vector = [0.0] * self.dimensions
        for token, count in counts.items():
            bucket, sign = self._hash(token)
            # Sub-linear TF: a term appearing 50 times is not 50x as important.
            vector[bucket % self.dimensions] += sign * (1.0 + math.log(count))

        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]


class SentenceTransformerEmbeddings(EmbeddingBackend):
    """Local semantic embeddings via ``sentence-transformers``.

    The model is loaded lazily on first use: constructing the backend must not
    pull ~90 MB of weights off disk during an import.
    """

    semantic = True

    def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L6-v2") -> None:
        self.name = model_name
        self._model: Any | None = None
        self.dimensions = 0

    @staticmethod
    def is_available() -> bool:
        """True when ``sentence-transformers`` can be imported."""
        try:
            import sentence_transformers  # noqa: F401
        except Exception:
            return False
        return True

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "sentence-transformers is not installed. Install it with "
                "pip install 'car-diagnostic-ai[rag-local-embeddings]', or let "
                "the service fall back to lexical hash embeddings."
            ) from exc
        log.info("Loading embedding model %s (first run downloads it)", self.name)
        self._model = SentenceTransformer(self.name)
        self.dimensions = int(self._model.get_sentence_embedding_dimension())
        return self._model

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._load()
        vectors = model.encode(list(texts), normalize_embeddings=True, show_progress_bar=False)
        return [[float(value) for value in vector] for vector in vectors]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


def build_embeddings(
    model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
    *,
    prefer_local_model: bool = True,
) -> EmbeddingBackend:
    """Pick the best embedding backend available in this environment.

    Never raises: a missing optional dependency degrades to
    :class:`HashEmbeddings` with a warning, because a diagnostic tool that
    refuses to start on a phone is of no use to anybody.
    """
    if prefer_local_model and SentenceTransformerEmbeddings.is_available():
        return SentenceTransformerEmbeddings(model_name)
    if prefer_local_model:
        log.warning(
            "sentence-transformers is unavailable - falling back to lexical hash "
            "embeddings. Retrieval will match on shared wording rather than "
            "meaning. Install 'car-diagnostic-ai[rag-local-embeddings]' for "
            "semantic search."
        )
    return HashEmbeddings()


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity in pure Python -- no numpy needed on Termux."""
    if len(left) != len(right):
        raise ValueError(
            f"Vector length mismatch: {len(left)} vs {len(right)}. This usually "
            f"means the index was built with a different embedding backend."
        )
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


__all__ = [
    "HASH_DIMENSIONS",
    "EmbeddingBackend",
    "HashEmbeddings",
    "SentenceTransformerEmbeddings",
    "build_embeddings",
    "cosine_similarity",
    "tokenize",
]
