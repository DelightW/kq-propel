"""
Recomputes the RAG-Triad figures over the full evaluation dataset using the
corrected metrics, and prints them beside the superseded values.

Offline and side-effect free: it does not write to the database.

Run:  python tools/recompute_metrics.py
"""
import json
import statistics
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app import config, evaluation, retrieval  # noqa: E402
from app.agent import SYSTEM_PROMPT, _serialize_context  # noqa: E402
from app.llm import get_alternate_llm, get_primary_llm  # noqa: E402
from app.vectorstore import get_vector_store  # noqa: E402

# Figures produced by the superseded overlap-based implementation.
SUPERSEDED = {
    "primary": {"context_relevance": 0.877, "groundedness": 1.0, "answer_relevance": 0.512},
    "alternate": {"context_relevance": 0.877, "groundedness": 1.0, "answer_relevance": 0.495},
}


def run() -> dict:
    dataset = json.loads(config.EVAL_DATASET_PATH.read_text(encoding="utf-8"))
    store = get_vector_store()
    models = {"primary": get_primary_llm(), "alternate": get_alternate_llm()}
    collected = {k: {"context_relevance": [], "groundedness": [], "answer_relevance": []}
                 for k in models}
    per_question = []

    for item in dataset:
        # The harness must present the retriever and the generator with exactly
        # what the agent presents them with. Serialising the context without the
        # [Source | Section] labels strips the cues the grounded composer relies
        # on, which measures a system that is never actually deployed.
        query = retrieval.focus_query(item["query"])
        retrieved = store.similarity_search(query, k=3)
        context_text = _serialize_context(retrieved)
        user_prompt = (f"CONTEXT:\n{context_text}\n\nTOOL OBSERVATIONS:\n\n"
                       f"QUESTION:\n{query}")
        row = {"id": item["id"], "query": item["query"]}
        for key, llm in models.items():
            answer = llm.generate(SYSTEM_PROMPT, user_prompt)
            metrics = evaluation.evaluate_response(
                item["query"], answer, retrieved,
                expected_keywords=item.get("expected_keywords"))
            for metric, value in metrics.items():
                collected[key][metric].append(value)
            row[key] = metrics
        per_question.append(row)

    summary = {
        key: {m: round(statistics.mean(v), 3) for m, v in metrics.items()}
        for key, metrics in collected.items()
    }
    return {"dataset_size": len(dataset), "summary": summary,
            "per_question": per_question,
            "models": {k: m.name for k, m in models.items()}}


def main() -> int:
    result = run()
    print("=" * 74)
    print(f"RAG-Triad recomputation  (n = {result['dataset_size']} questions)")
    print("=" * 74)

    for key, name in result["models"].items():
        print(f"\n{key.upper()}  -  {name}")
        print(f"  {'metric':<20} {'superseded':>12} {'corrected':>12} {'delta':>10}")
        for metric in ("context_relevance", "groundedness", "answer_relevance"):
            old = SUPERSEDED[key][metric]
            new = result["summary"][key][metric]
            print(f"  {metric:<20} {old:>12.3f} {new:>12.3f} {new - old:>+10.3f}")
        grounded = result["summary"][key]["groundedness"]
        print(f"  {'hallucination_rate':<20} {1 - SUPERSEDED[key]['groundedness']:>12.3f} "
              f"{round(1 - grounded, 3):>12.3f} {(1 - grounded) - (1 - SUPERSEDED[key]['groundedness']):>+10.3f}")

    weakest = min(
        ((m, v) for m, v in result["summary"]["primary"].items()),
        key=lambda kv: kv[1])
    print(f"\nWeakest corrected metric (primary): {weakest[0]} = {weakest[1]}")

    out = Path(__file__).resolve().parent / "recomputed_metrics.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Full per-question detail written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
