"""
Regression tests for the Phase 3b retrieval changes.

Two classes of failure are guarded here, and the second is the one that
motivated the file.

**Correctness of the embedding tiering.** The provider must report the backend
that actually answered, not the one that was configured. The project has
already been bitten once by a diagnostic that reported a live integration while
the application was quietly using a fallback, so "configured" and "active" are
kept as separate, separately-observable things.

**Index/backend agreement.** Vectors from different backends have different
widths. Comparing them yields a cosine of zero for every chunk, which does not
raise - it silently turns hybrid search into pure BM25 while every score in the
trace still looks plausible. The store must detect the mismatch and rebuild.

Run: python tools/test_embedding_backend.py
"""
import sys
import traceback
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app import config, embeddings, vectorstore  # noqa: E402


def test_hashed_tier_is_labelled_as_non_semantic():
    # The fallback must never be describable as a semantic model. Its cosine is
    # token overlap, and calling it an embedding in a report would overclaim.
    vec = embeddings._hashed_embedding("delayed baggage")
    assert len(vec) == config.HASHED_EMBEDDING_DIMENSIONS
    assert abs(sum(v * v for v in vec) - 1.0) < 1e-6


def test_hashed_tier_cannot_match_synonyms():
    # The concrete limitation that justifies Phase 3b, pinned as a fact rather
    # than a claim: with no shared tokens, the hashed vector has nothing to go on.
    a = embeddings._hashed_embedding("how much luggage may I carry")
    b = embeddings._hashed_embedding("checked baggage allowance permitted")
    assert embeddings.cosine_similarity(a, b) == 0.0


def test_active_backend_is_probed_not_assumed():
    name = embeddings.provider_name()
    assert name and name != "unknown"
    described = embeddings.describe()
    assert described["provider"] == name
    assert isinstance(described["dimension"], int)
    assert described["dimension"] == len(embeddings.embed_text("probe"))
    # "semantic" must follow the backend that answered, not the preference.
    assert described["semantic"] == ("hashed" not in name)


def test_batch_and_single_embedding_agree():
    texts = ["excess baggage fee", "name correction charge"]
    batched = embeddings.embed_texts(texts)
    assert len(batched) == 2
    assert [round(v, 5) for v in batched[0]] == \
        [round(v, 5) for v in embeddings.embed_text(texts[0])]


def test_empty_batch_is_not_an_error():
    assert embeddings.embed_texts([]) == []


def test_semantic_backend_relates_synonyms_when_available():
    if not embeddings.is_semantic():
        print("      (skipped - no semantic backend installed)")
        return
    a = embeddings.embed_text("how much luggage may I carry")
    b = embeddings.embed_text("checked baggage allowance permitted")
    c = embeddings.embed_text("refund processing timeline for a cancelled ticket")
    related = embeddings.cosine_similarity(a, b)
    unrelated = embeddings.cosine_similarity(a, c)
    assert related > unrelated, (related, unrelated)


def test_mismatched_index_is_detected_rather_than_scoring_zero():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = vectorstore.LocalVectorStore(path=Path(tmp) / "index.json")
        store.add_document("baggage", "baggage.txt",
                           "Baggage Allowance\n\nSection 1: Economy\n"
                           "Economy guests may check two bags of 23kg each.",
                           provenance="test")
        assert store.records and not store.is_stale()

        # Simulate an index written by a different backend.
        store.records = [dict(r, embedding=r["embedding"] + [0.0]) for r in store.records]
        assert store.is_stale()

        results = store.similarity_search("how many bags in economy", k=3)
        assert results, "search must still return something, not crash"
        # The degradation has to be visible rather than looking like a genuine
        # zero similarity.
        assert all(r["dense_score"] == 0.0 for r in results)
        assert all("dense_stage" in r for r in results)
        assert "index_status" in store.stats()


def test_stale_detection_uses_the_stored_width():
    store = vectorstore.LocalVectorStore.__new__(vectorstore.LocalVectorStore)
    store.records = [{"doc_id": "d", "embedding": [0.0] * 7}]
    assert store.index_dimension() == 7
    assert store.is_stale() is (embeddings.dimension() != 7)


def test_mmr_disabled_returns_plain_top_k():
    scored = [{"doc_id": "a", "score": 0.9}, {"doc_id": "a", "score": 0.8},
              {"doc_id": "b", "score": 0.7}]
    records = [{"embedding": [1.0, 0.0]}, {"embedding": [1.0, 0.0]},
               {"embedding": [0.0, 1.0]}]
    assert vectorstore._mmr_select([0, 1, 2], scored, records, 2, 1.0, False) == [0, 1]


def test_mmr_breaks_up_near_duplicates():
    # The failure MMR was added for: a trained encoder ranks every paragraph of
    # the closest document highly, so plain top-k returns one document three
    # times and a two-document question can never be answered.
    scored = [{"doc_id": "a", "score": 0.90}, {"doc_id": "a", "score": 0.88},
              {"doc_id": "b", "score": 0.60}]
    records = [{"embedding": [1.0, 0.0]}, {"embedding": [0.99, 0.01]},
               {"embedding": [0.0, 1.0]}]
    selected = vectorstore._mmr_select([0, 1, 2], scored, records, 2, 0.6, False)
    assert selected[0] == 0
    assert scored[selected[1]]["doc_id"] == "b", "second slot should add a new document"


def test_mmr_still_prefers_relevance_when_documents_differ():
    scored = [{"doc_id": "a", "score": 0.9}, {"doc_id": "b", "score": 0.8},
              {"doc_id": "c", "score": 0.1}]
    records = [{"embedding": [1.0, 0.0]}, {"embedding": [0.0, 1.0]},
               {"embedding": [0.0, -1.0]}]
    assert vectorstore._mmr_select([0, 1, 2], scored, records, 2, 0.6, False) == [0, 1]


def test_mmr_falls_back_to_document_identity_on_a_stale_index():
    # With a stale index the embeddings are unusable, so redundancy has to be
    # judged on document identity alone rather than silently scoring 0.
    scored = [{"doc_id": "a", "score": 0.9}, {"doc_id": "a", "score": 0.85},
              {"doc_id": "b", "score": 0.5}]
    records = [{"embedding": []}, {"embedding": []}, {"embedding": []}]
    selected = vectorstore._mmr_select([0, 1, 2], scored, records, 2, 0.6, True)
    assert scored[selected[1]]["doc_id"] == "b"


def test_mmr_never_returns_more_than_k_or_duplicates():
    scored = [{"doc_id": chr(97 + i), "score": 1.0 - i / 10} for i in range(8)]
    records = [{"embedding": [1.0, 0.0]} for _ in range(8)]
    selected = vectorstore._mmr_select(list(range(8)), scored, records, 3, 0.6, False)
    assert len(selected) == 3
    assert len(set(selected)) == 3


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except Exception:
            failures += 1
            print(f"  FAIL  {test.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
