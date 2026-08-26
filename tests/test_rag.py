"""Workshop-manual embedding, chunking, storage and retrieval."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from majster_ai.config import load_settings
from majster_ai.errors import ConfigError, RagError
from majster_ai.mcp_servers.rag_workshop.embeddings import (
    HashEmbeddings,
    SentenceTransformerEmbeddings,
    build_embeddings,
    cosine_similarity,
    tokenize,
)
from majster_ai.mcp_servers.rag_workshop.ingest import (
    chunk_id,
    discover_manuals,
    documents_from_directory,
    normalise_text,
    split_text,
    strip_html,
)
from majster_ai.mcp_servers.rag_workshop.service import RagService
from majster_ai.mcp_servers.rag_workshop.store import (
    Document,
    InMemoryVectorStore,
    build_store,
)


class TestHashEmbeddings:
    def test_fixed_dimensionality(self) -> None:
        embeddings = HashEmbeddings(256)
        assert len(embeddings.embed_query("anything at all")) == 256

    def test_normalised(self) -> None:
        vector = HashEmbeddings().embed_query("turbocharger actuator")
        assert sum(value * value for value in vector) == pytest.approx(1.0, abs=1e-6)

    def test_deterministic_within_a_process(self) -> None:
        embeddings = HashEmbeddings()
        assert embeddings.embed_query("dpf") == embeddings.embed_query("dpf")

    def test_deterministic_across_processes(self) -> None:
        """Python randomises str hashing per process; using it would silently
        invalidate a persisted index on every restart."""
        first = HashEmbeddings().embed_query("swirl flap")
        second = HashEmbeddings().embed_query("swirl flap")
        assert first == second

    def test_empty_text(self) -> None:
        assert HashEmbeddings().embed_query("") == [0.0] * 512

    def test_shared_wording_scores_higher(self) -> None:
        embeddings = HashEmbeddings()
        query = embeddings.embed_query("turbocharger actuator underboost")
        related = embeddings.embed_query("the turbocharger actuator causes underboost")
        unrelated = embeddings.embed_query("brake caliper bolt torque specification")
        assert cosine_similarity(query, related) > cosine_similarity(query, unrelated)

    def test_reports_itself_as_lexical(self) -> None:
        assert HashEmbeddings().semantic is False

    def test_rejects_tiny_dimensionality(self) -> None:
        with pytest.raises(ValueError):
            HashEmbeddings(4)


class TestTokenize:
    def test_keeps_automotive_terms_whole(self) -> None:
        tokens = tokenize("ISO-TP P0299 EGR/DPF fault")
        assert "iso-tp" in tokens and "p0299" in tokens and "egr/dpf" in tokens

    def test_drops_stopwords(self) -> None:
        assert "the" not in tokenize("the turbocharger")


class TestBackendSelection:
    def test_falls_back_without_sentence_transformers(self, monkeypatch) -> None:
        monkeypatch.setattr(
            SentenceTransformerEmbeddings, "is_available", staticmethod(lambda: False)
        )
        assert isinstance(build_embeddings(), HashEmbeddings)

    def test_never_raises(self) -> None:
        # A diagnostic tool that will not start on a phone is of no use.
        assert build_embeddings("no-such-model/at-all") is not None


class TestCosine:
    def test_identical(self) -> None:
        assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)

    def test_orthogonal(self) -> None:
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_zero_vector(self) -> None:
        assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0

    def test_length_mismatch_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="length mismatch"):
            cosine_similarity([1.0], [1.0, 2.0])


class TestChunking:
    def test_short_text_is_one_chunk(self) -> None:
        assert split_text("short", 100, 10) == ["short"]

    @pytest.mark.parametrize("size,overlap", [(80, 0), (150, 20), (400, 50), (1200, 200)])
    def test_never_exceeds_the_limit(self, size: int, overlap: int) -> None:
        # An embedding model with a hard token limit silently truncates
        # anything longer, dropping the end of every chunk without warning.
        text = "\n\n".join("Sentence one. Sentence two. " * 10 for _ in range(8))
        assert max(len(chunk) for chunk in split_text(text, size, overlap)) <= size

    def test_no_duplication(self) -> None:
        text = "\n".join(f"LINE{i:02d} " + "x" * 60 for i in range(20))
        joined = " ".join(split_text(text, 150, 0))
        for index in range(20):
            assert len(re.findall(f"LINE{index:02d}", joined)) == 1

    def test_content_is_preserved(self) -> None:
        text = "\n\n".join("Sentence one. Sentence two. " * 8 for _ in range(6))
        rebuilt = "".join(split_text(text, 200, 0))
        assert re.sub(r"\s+", "", rebuilt) == re.sub(r"\s+", "", text)

    def test_overlap_carries_context_forward(self) -> None:
        # A procedure split across a boundary must be findable from either
        # side, so each chunk after the first must open with text that also
        # appears at the end of its predecessor.
        overlap = 60
        chunks = split_text("Check the VGT actuator. " * 100, 200, overlap)
        assert len(chunks) > 1
        for previous, current in zip(chunks, chunks[1:]):
            head = current[:20]
            assert (
                head and head in previous
            ), f"chunk starting {head!r} shares no text with its predecessor"

    def test_zero_overlap_shares_nothing(self) -> None:
        # Distinct content per sentence, so a shared prefix can only come from
        # overlap and not from the text repeating itself.
        text = " ".join(f"Step {i:03d} tighten bolt {i:03d} fully." for i in range(200))
        chunks = split_text(text, 200, 0)
        assert len(chunks) > 1
        assert chunks[1][:20] not in chunks[0]

    def test_unbroken_text_still_splits(self) -> None:
        chunks = split_text("y" * 5000, 400, 50)
        assert len(chunks) > 1 and max(len(c) for c in chunks) <= 400

    @pytest.mark.parametrize("args", [(0, 0), (100, 100), (100, 150), (100, -1)])
    def test_invalid_parameters_rejected(self, args) -> None:
        with pytest.raises(RagError):
            split_text("abc" * 200, *args)

    def test_chunk_ids_are_stable(self) -> None:
        # Re-ingesting must update, not duplicate.
        assert chunk_id("m.pdf", 1, 0, "abc") == chunk_id("m.pdf", 1, 0, "abc")
        assert chunk_id("m.pdf", 1, 0, "abc") != chunk_id("m.pdf", 2, 0, "abc")


class TestTextHandling:
    def test_normalise_collapses_pdf_whitespace(self) -> None:
        assert normalise_text("a   b\r\n\n\n\nc") == "a b\n\nc"

    def test_strip_html_removes_scripts(self) -> None:
        text = strip_html("<html><script>evil()</script><p>Torque: 30 Nm</p></html>")
        assert text == "Torque: 30 Nm"


class TestDiscovery:
    def test_finds_supported_files(self, manuals_dir: Path) -> None:
        assert len(discover_manuals(manuals_dir)) == 2

    def test_ignores_unsupported(self, manuals_dir: Path) -> None:
        (manuals_dir / "notes.docx").write_text("x")
        assert len(discover_manuals(manuals_dir)) == 2

    def test_recurses(self, manuals_dir: Path) -> None:
        nested = manuals_dir / "sub"
        nested.mkdir()
        (nested / "extra.md").write_text("Extra content")
        assert len(discover_manuals(manuals_dir)) == 3

    def test_missing_directory(self, tmp_path: Path) -> None:
        assert discover_manuals(tmp_path / "nope") == []

    def test_documents_carry_source_metadata(self, manuals_dir: Path) -> None:
        documents = list(documents_from_directory(manuals_dir, chunk_size=200, chunk_overlap=20))
        assert documents
        assert all(doc.metadata.get("filename") for doc in documents)


class TestStores:
    @pytest.fixture
    def documents(self) -> list[Document]:
        return [
            Document(
                id="d1",
                text="Turbocharger underboost P0299 VGT actuator.",
                metadata={"source": "m.pdf", "page": 42},
            ),
            Document(
                id="d2",
                text="Brake caliper bolt torque is 30 Nm.",
                metadata={"source": "m.pdf", "page": 88},
            ),
        ]

    @pytest.mark.parametrize("kind", ["memory", "chroma"])
    def test_add_and_search(self, kind: str, tmp_path: Path, documents) -> None:
        embeddings = HashEmbeddings()
        store = (
            InMemoryVectorStore(tmp_path / "i.json", embeddings.name)
            if kind == "memory"
            else build_store(tmp_path / "chroma", "workshop_manuals", embeddings.name)
        )
        store.add_documents(documents, embeddings.embed_documents([d.text for d in documents]))
        assert store.count() == 2
        results = store.search(embeddings.embed_query("turbocharger underboost"), 2)
        assert results[0].document.text.startswith("Turbocharger")

    def test_citations_name_file_and_page(self, documents) -> None:
        assert documents[0].citation() == "m.pdf, page 42"

    def test_citation_without_a_page(self) -> None:
        assert Document("x", "t", {"source": "a/b/m.txt"}).citation() == "m.txt"

    def test_persistence_round_trip(self, tmp_path: Path, documents) -> None:
        embeddings = HashEmbeddings()
        path = tmp_path / "index.json"
        store = InMemoryVectorStore(path, embeddings.name)
        store.add_documents(documents, embeddings.embed_documents([d.text for d in documents]))
        store.save()
        assert InMemoryVectorStore(path).count() == 2

    def test_corrupt_index_starts_empty_rather_than_crashing(self, tmp_path: Path) -> None:
        path = tmp_path / "index.json"
        path.write_text("{corrupt")
        assert InMemoryVectorStore(path).count() == 0

    def test_duplicate_ids_are_not_added_twice(self, documents) -> None:
        embeddings = HashEmbeddings()
        store = InMemoryVectorStore(None, embeddings.name)
        vectors = embeddings.embed_documents([d.text for d in documents])
        store.add_documents(documents, vectors)
        store.add_documents(documents, vectors)
        assert store.count() == 2

    def test_count_mismatch_rejected(self, documents) -> None:
        with pytest.raises(RagError, match="mismatch"):
            InMemoryVectorStore(None).add_documents(documents, [[0.1] * 512])

    def test_backend_mismatch_is_detected(self, documents) -> None:
        # Vectors from different backends are not comparable; searching anyway
        # would return confident nonsense.
        store = InMemoryVectorStore(None, "some-other-backend")
        store.add_documents(documents[:1], [[0.1] * 512])
        with pytest.raises(RagError, match="not comparable"):
            store.check_compatible(HashEmbeddings())

    def test_empty_index_is_always_compatible(self) -> None:
        InMemoryVectorStore(None, "anything").check_compatible(HashEmbeddings())

    def test_clear(self, documents) -> None:
        embeddings = HashEmbeddings()
        store = InMemoryVectorStore(None, embeddings.name)
        store.add_documents(documents, embeddings.embed_documents([d.text for d in documents]))
        store.clear()
        assert store.count() == 0


class TestRagService:
    @pytest.fixture
    def service(self, tmp_path: Path, manuals_dir: Path) -> RagService:
        settings = load_settings(
            manuals_dir=manuals_dir,
            vector_dir=tmp_path / "vs",
            rag_chunk_size=400,
            rag_chunk_overlap=50,
            log_level="CRITICAL",
        )
        return RagService(
            settings,
            embeddings=HashEmbeddings(),
            store=InMemoryVectorStore(None, HashEmbeddings().name),
        )

    def test_search_before_ingest_says_so(self, service: RagService) -> None:
        result = service.search_manual("P0299")
        assert result["error"] == "index_not_built"
        assert "ingest" in result["message"]

    def test_ingest_then_search(self, service: RagService) -> None:
        assert service.ingest()["ok"] is True
        result = service.search_manual("swirl flap linkage wear")
        assert result["ok"] is True and result["count"] > 0

    def test_results_carry_citations(self, service: RagService) -> None:
        service.ingest()
        result = service.search_manual("turbocharger actuator")
        assert result["citations"]
        assert all(entry["citation"] for entry in result["results"])

    def test_lexical_backend_declares_its_limitation(self, service: RagService) -> None:
        service.ingest()
        assert "lexical" in service.search_manual("turbocharger")["retrieval_caveat"]

    def test_source_filter(self, service: RagService) -> None:
        service.ingest()
        result = service.search_manual("torque", source_filter="driveline")
        assert all("driveline" in entry["source"] for entry in result["results"])

    def test_no_match_suggests_what_to_do(self, service: RagService) -> None:
        service.ingest()
        result = service.search_manual("zzzz quantum flux capacitor alignment")
        assert result["count"] == 0
        assert "web" in result["summary"] or "wording" in result["summary"]

    def test_empty_query(self, service: RagService) -> None:
        assert service.search_manual("")["error"] == "empty_query"

    def test_reingest_is_idempotent(self, service: RagService) -> None:
        first = service.ingest()["total_in_index"]
        assert service.ingest()["total_in_index"] == first

    def test_rebuild(self, service: RagService) -> None:
        service.ingest()
        assert service.ingest(rebuild=True)["ok"] is True

    def test_missing_manuals_dir(self, tmp_path: Path) -> None:
        settings = load_settings(
            manuals_dir=tmp_path / "nope", vector_dir=tmp_path / "vs", log_level="CRITICAL"
        )
        result = RagService(
            settings, embeddings=HashEmbeddings(), store=InMemoryVectorStore(None)
        ).ingest()
        assert result["error"] == "manuals_dir_missing"

    def test_empty_manuals_dir(self, tmp_path: Path) -> None:
        empty = tmp_path / "manuals"
        empty.mkdir()
        settings = load_settings(
            manuals_dir=empty, vector_dir=tmp_path / "vs", log_level="CRITICAL"
        )
        result = RagService(
            settings, embeddings=HashEmbeddings(), store=InMemoryVectorStore(None)
        ).ingest()
        assert result["error"] == "no_manuals_found"

    def test_status(self, service: RagService) -> None:
        assert service.status()["ready"] is False
        service.ingest()
        assert service.status()["ready"] is True

    def test_list_sources(self, service: RagService) -> None:
        service.ingest()
        names = service.list_sources()["sources"]
        assert "fl2_engine.md" in names

    def test_corrupt_file_is_skipped_not_fatal(
        self, service: RagService, manuals_dir: Path
    ) -> None:
        # One bad PDF must not prevent the rest of the library being indexed.
        (manuals_dir / "broken.pdf").write_bytes(b"this is not a pdf at all")
        result = service.ingest()
        assert result["ok"] is True
        assert result["chunks_added"] > 0
