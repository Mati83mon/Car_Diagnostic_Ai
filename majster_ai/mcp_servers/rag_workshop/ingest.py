"""Loading and chunking workshop manuals.

Supported inputs: PDF (via ``pypdf``), plain text, Markdown, and HTML. PDFs are
the realistic case -- a JLR workshop manual is a few thousand pages -- so page
numbers are carried through into chunk metadata. Being able to say "page 412 of
the manual" rather than "somewhere in the manual" is the difference between a
citation and an assertion.

Chunk ids are deterministic (a hash of source, page and offset), so re-running
ingestion updates the index in place instead of duplicating every chunk.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Final, Iterable, Iterator, Sequence

from majster_ai.errors import RagError
from majster_ai.logging_setup import get_logger
from majster_ai.mcp_servers.rag_workshop.store import Document

log = get_logger("mcp_servers.rag_workshop.ingest")

#: Extensions we know how to read.
SUPPORTED_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {".pdf", ".txt", ".md", ".markdown", ".html", ".htm"}
)

#: Split points, tried in order: paragraph, line, sentence, word, character.
_SPLIT_SEPARATORS: Final[tuple[str, ...]] = ("\n\n", "\n", ". ", " ", "")

_HTML_TAG_RE: Final = re.compile(r"<[^>]+>")
_HTML_SCRIPT_RE: Final = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_WHITESPACE_RE: Final = re.compile(r"[ \t ]+")
_BLANKLINE_RE: Final = re.compile(r"\n{3,}")


def normalise_text(text: str) -> str:
    """Collapse the whitespace damage that PDF extraction always causes."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WHITESPACE_RE.sub(" ", text)
    text = _BLANKLINE_RE.sub("\n\n", text)
    return text.strip()


def strip_html(markup: str) -> str:
    """Crude but dependency-free HTML to text."""
    without_scripts = _HTML_SCRIPT_RE.sub(" ", markup)
    return normalise_text(_HTML_TAG_RE.sub(" ", without_scripts))


def _split_recursive(segment: str, separators: Sequence[str], budget: int) -> list[str]:
    """Split ``segment`` into pieces of at most ``budget`` characters.

    Walks ``separators`` from coarsest to finest (paragraph, line, sentence,
    word, character), descending only when a piece is still too large. A table
    with no blank lines therefore still gets split rather than becoming one
    enormous chunk.
    """
    if len(segment) <= budget:
        return [segment] if segment.strip() else []
    if not separators or separators[0] == "":
        return [segment[i : i + budget] for i in range(0, len(segment), budget)]

    separator, rest = separators[0], separators[1:]
    # Re-attach the separator to the part it followed. Plain str.split() eats
    # it, which silently deletes the full stop at every sentence-level chunk
    # boundary and the blank line at every paragraph one.
    raw_parts = segment.split(separator)
    parts = [piece + separator for piece in raw_parts[:-1]] + raw_parts[-1:]

    chunks: list[str] = []
    buffer = ""
    for part in parts:
        candidate = buffer + part if buffer else part
        if len(candidate) <= budget:
            buffer = candidate
            continue

        # The candidate overflows: emit what we have and start again.
        if buffer:
            chunks.append(buffer)
            buffer = ""
        if len(part) > budget:
            # Too big even alone -- descend to a finer separator. Note we do
            # NOT also buffer it; doing both is how the same text ends up
            # indexed twice.
            chunks.extend(_split_recursive(part, rest, budget))
        else:
            buffer = part

    if buffer.strip():
        chunks.append(buffer)
    return [chunk for chunk in chunks if chunk.strip()]


def _apply_overlap(chunks: list[str], chunk_overlap: int, chunk_size: int) -> list[str]:
    """Prepend a tail of each chunk to its successor.

    Keeps a procedure that straddles a boundary findable from either side.
    """
    overlapped: list[str] = [chunks[0]]
    for previous, current in zip(chunks, chunks[1:]):
        tail = previous[-chunk_overlap:].strip()
        if not tail:
            overlapped.append(current)
            continue
        combined = f"{tail} {current}"
        # The joining space costs a character; trim the tail rather than let
        # the chunk creep past the limit.
        if len(combined) > chunk_size:
            combined = combined[len(combined) - chunk_size :].lstrip()
        overlapped.append(combined)
    return overlapped


def split_text(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    """Split text into chunks that respect natural boundaries.

    Overlap is taken from the *budget*, not added on top: pieces are cut to
    ``chunk_size - chunk_overlap`` and then the tail of the previous piece is
    prepended, so no returned chunk ever exceeds ``chunk_size``. An embedding
    model with a hard token limit silently truncates anything longer, which
    would drop the end of every chunk without a word of warning.

    Raises:
        RagError: on nonsensical chunk parameters.
    """
    if chunk_size <= 0:
        raise RagError("chunk_size must be positive")
    if chunk_overlap < 0:
        raise RagError("chunk_overlap must not be negative")
    if chunk_overlap >= chunk_size:
        raise RagError("chunk_overlap must be smaller than chunk_size")

    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    chunks = _split_recursive(text, _SPLIT_SEPARATORS, chunk_size - chunk_overlap)
    if chunk_overlap <= 0 or len(chunks) < 2:
        return chunks
    return _apply_overlap(chunks, chunk_overlap, chunk_size)


def chunk_id(source: str, page: int | None, index: int, text: str) -> str:
    """A stable id, so re-ingesting updates rather than duplicates."""
    digest = hashlib.blake2b(
        f"{source}|{page}|{index}|{text[:256]}".encode("utf-8"), digest_size=12
    ).hexdigest()
    return f"wm-{digest}"


def load_pdf_pages(path: Path) -> Iterator[tuple[int, str]]:
    """Yield ``(page_number, text)`` for each page of a PDF.

    A page that will not extract is logged and skipped: one malformed page in
    a 3000-page manual must not abort the whole ingest.
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RagError(
            "pypdf is required to read PDF manuals. Install it with "
            "pip install 'car-diagnostic-ai[rag]'"
        ) from exc

    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        raise RagError(f"Cannot open the PDF {path.name}: {exc}", source=str(path)) from exc

    for number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:
            log.warning("Skipping page %d of %s: %s", number, path.name, exc)
            continue
        cleaned = normalise_text(text)
        if cleaned:
            yield number, cleaned


def load_file(path: Path) -> Iterator[tuple[int | None, str]]:
    """Yield ``(page, text)`` for any supported file type."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        yield from load_pdf_pages(path)
        return

    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RagError(f"Cannot read {path.name}: {exc}", source=str(path)) from exc

    text = strip_html(raw) if suffix in (".html", ".htm") else normalise_text(raw)
    if text:
        yield None, text


def discover_manuals(directory: str | Path) -> list[Path]:
    """Find every supported document under ``directory``, recursively."""
    root = Path(directory)
    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )


def documents_from_file(
    path: Path, *, chunk_size: int = 1200, chunk_overlap: int = 200
) -> Iterator[Document]:
    """Load one file and yield its chunks as :class:`Document` objects."""
    source = str(path)
    for page, text in load_file(path):
        for index, chunk in enumerate(split_text(text, chunk_size, chunk_overlap)):
            metadata: dict[str, Any] = {
                "source": source,
                "filename": path.name,
                "chunk_index": index,
            }
            if page is not None:
                metadata["page"] = page
            yield Document(id=chunk_id(source, page, index, chunk), text=chunk, metadata=metadata)


def documents_from_directory(
    directory: str | Path,
    *,
    chunk_size: int = 1200,
    chunk_overlap: int = 200,
    files: Iterable[Path] | None = None,
) -> Iterator[Document]:
    """Load and chunk every manual in a directory.

    A file that fails to load is logged and skipped, so one corrupt PDF cannot
    prevent the rest of the library from being indexed.
    """
    for path in (list(files) if files is not None else discover_manuals(directory)):
        log.info("Ingesting %s", path.name)
        try:
            yield from documents_from_file(path, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        except RagError as exc:
            log.error("Skipping %s: %s", path.name, exc.message)


__all__ = [
    "SUPPORTED_EXTENSIONS",
    "normalise_text",
    "strip_html",
    "split_text",
    "chunk_id",
    "load_pdf_pages",
    "load_file",
    "discover_manuals",
    "documents_from_file",
    "documents_from_directory",
]
