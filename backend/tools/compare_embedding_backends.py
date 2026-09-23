"""
A/B comparison of the embedding backends behind the dense retrieval stage.

Phase 3b replaced a hashed bag-of-words vector with a real sentence-transformer
encoder. This script exists so that change is defended with a measurement
rather than an assertion: it rebuilds the index under each backend, replays the
full evaluation set, and reports the difference.

Each backend runs in its own subprocess. That is not incidental - `config` reads
its environment once at import, and both the model and the vector store are
process-level singletons, so switching backends inside one process would measure
a mixture of the two.

Run:  python tools/compare_embedding_backends.py
"""
import json
import os
import statistics
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

BACKENDS = [
    ("hashed", "false", "hashed bag-of-words (pre-Phase-3b)"),
    ("minilm", "true", "sentence-transformers/all-MiniLM-L6-v2"),
]


def _child() -> int:
    """Runs inside the subprocess: rebuild, evaluate, emit one JSON line."""
    from app import config, embeddings, evaluation, retrieval
    from app.agent import SYSTEM_PROMPT, _serialize_context
    from app.llm import get_primary_llm
    from app.vectorstore import get_vector_store, ingest_policy_directory

    ingest_policy_directory(rebuild=True)
    store = get_vector_store()
    llm = get_primary_llm()
    dataset = json.loads(config.EVAL_DATASET_PATH.read_text(encoding="utf-8"))

    triad = {"context_relevance": [], "groundedness": [], "answer_relevance": []}
    dense_top = []
    covered_any = 0
    covered_all = 0
    multi_hop_total = 0
    multi_hop_covered = 0
    per_question = []

    for item in dataset:
        query = retrieval.focus_query(item["query"])
        retrieved = store.similarity_search(query, k=3)
        if retrieved:
            dense_top.append(retrieved[0].get("dense_score", 0.0))
        answer = llm.generate(
            SYSTEM_PROMPT,
            f"CONTEXT:\n{_serialize_context(retrieved)}\n\nTOOL OBSERVATIONS:\n\n"
            f"QUESTION:\n{query}")
        metrics = evaluation.evaluate_response(
            item["query"], answer, retrieved,
            expected_keywords=item.get("expected_keywords"))
        for metric, value in metrics.items():
            triad[metric].append(value)

        # Did the documents the question is actually about reach the context?
        # `expected_topics` holds doc_ids, so this is measurable independently
        # of how the answer was worded.
        expected = set(item.get("expected_topics") or [])
        got = {r.get("doc_id") for r in retrieved}
        if expected:
            if expected & got:
                covered_any += 1
            if expected <= got:
                covered_all += 1
            if len(expected) > 1:
                multi_hop_total += 1
                if expected <= got:
                    multi_hop_covered += 1

        per_question.append({
            "id": item["id"],
            "query": item["query"],
            "expected": sorted(expected),
            "retrieved": [r.get("doc_id") for r in retrieved],
            "context_relevance": metrics.get("context_relevance"),
            "answer_relevance": metrics.get("answer_relevance"),
        })

    n = len(dataset) or 1
    payload = {
        "provider": embeddings.provider_name(),
        "dimension": embeddings.dimension(),
        "semantic": embeddings.is_semantic(),
        "questions": len(dataset),
        "triad": {m: round(statistics.mean(v), 3) for m, v in triad.items()},
        "mean_top_dense_score": round(statistics.mean(dense_top), 3) if dense_top else 0.0,
        "any_expected_doc_at_3": round(covered_any / n, 3),
        "all_expected_docs_at_3": round(covered_all / n, 3),
        "multi_hop_fully_covered_at_3": (round(multi_hop_covered / multi_hop_total, 3)
                                         if multi_hop_total else None),
        "multi_hop_questions": multi_hop_total,
        "zero_score_questions": sum(1 for v in triad["answer_relevance"] if v == 0.0),
        "per_question": per_question,
    }
    print("RESULT_JSON " + json.dumps(payload))
    return 0


def _run_backend(enabled: str) -> dict:
    env = dict(os.environ)
    env["LOCAL_EMBEDDING_ENABLED"] = enabled
    env["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
    env["KQ_COMPARE_CHILD"] = "1"
    proc = subprocess.run([sys.executable, __file__], cwd=str(BACKEND), env=env,
                          capture_output=True, text=True)
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT_JSON "):
            return json.loads(line[len("RESULT_JSON "):])
    raise RuntimeError(f"child produced no result\nstdout:\n{proc.stdout}\n"
                       f"stderr:\n{proc.stderr[-2000:]}")


def main() -> int:
    results = {}
    for name, enabled, label in BACKENDS:
        print(f"running {label} ...", flush=True)
        results[name] = _run_backend(enabled)

    hashed, minilm = results["hashed"], results["minilm"]

    print()
    print("=" * 78)
    print("Dense retrieval backend comparison - identical corpus, questions and")
    print("generator; only the embedding differs.")
    print("=" * 78)
    for name in ("hashed", "minilm"):
        r = results[name]
        print(f"  {name:8} {r['provider']}  ({r['dimension']}d, "
              f"semantic={r['semantic']})")
    print(f"\n  questions: {minilm['questions']}\n")

    rows = [
        ("context_relevance", hashed["triad"]["context_relevance"],
         minilm["triad"]["context_relevance"]),
        ("groundedness", hashed["triad"]["groundedness"],
         minilm["triad"]["groundedness"]),
        ("answer_relevance", hashed["triad"]["answer_relevance"],
         minilm["triad"]["answer_relevance"]),
        ("any expected doc @3", hashed["any_expected_doc_at_3"],
         minilm["any_expected_doc_at_3"]),
        ("all expected docs @3", hashed["all_expected_docs_at_3"],
         minilm["all_expected_docs_at_3"]),
        ("multi-hop covered @3", hashed["multi_hop_fully_covered_at_3"] or 0.0,
         minilm["multi_hop_fully_covered_at_3"] or 0.0),
        ("mean top dense score", hashed["mean_top_dense_score"],
         minilm["mean_top_dense_score"]),
        ("questions scoring zero", hashed["zero_score_questions"],
         minilm["zero_score_questions"]),
    ]
    print(f"  {'measure':<24}{'hashed':>10}{'MiniLM':>10}{'delta':>10}")
    for label, a, b in rows:
        print(f"  {label:<24}{a:>10}{b:>10}{b - a:>+10.3f}")

    print("\n  'mean top dense score' is not a quality measure - it only shows the")
    print("  two backends put cosine on different scales. The blend min-max")
    print("  normalises before combining, so what matters is the ranking measures")
    print("  above it.")

    # Per-question coverage diff. A headline average can hide a backend that
    # wins on many questions while breaking a few, so name the individual
    # questions each backend gained and lost.
    by_id = {r["id"]: r for r in hashed["per_question"]}
    gained, lost = [], []
    for row in minilm["per_question"]:
        before = by_id.get(row["id"])
        if not before or not row["expected"]:
            continue
        exp = set(row["expected"])
        was = exp & set(before["retrieved"])
        now = exp & set(row["retrieved"])
        if len(now) > len(was):
            gained.append((row, before))
        elif len(now) < len(was):
            lost.append((row, before))

    def _show(title, items):
        print(f"\n  {title} ({len(items)})")
        if not items:
            print("    none")
        for row, before in items:
            print(f"    {row['id']}: {row['query'][:64]}")
            print(f"      expected : {row['expected']}")
            print(f"      hashed   : {before['retrieved']}")
            print(f"      MiniLM   : {row['retrieved']}")

    _show("questions MiniLM newly covers", gained)
    _show("questions MiniLM lost", lost)

    out = BACKEND / "tools" / "embedding_backend_comparison.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\n  detail written to {out}")

    print("\n  Note: the index on disk is now the one built by the backend that")
    print("  ran last (MiniLM). Nothing further is required - a mismatched index")
    print("  is detected and rebuilt automatically on the next ingestion.")
    return 0


if __name__ == "__main__":
    if os.getenv("KQ_COMPARE_CHILD"):
        raise SystemExit(_child())
    raise SystemExit(main())
