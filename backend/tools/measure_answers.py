"""
Measures the agent's own answer path over the evaluation set: the same
focusing, retrieval depth, prompt and offline composer the agent uses.

Prints the RAG-Triad means, any@k / all@k document recall, and every question
whose answer misses a gold key. Use it to accept or reject a retrieval change:
run it before and after, and keep the change only if the figures improve.

    python tools/measure_answers.py            # k=4, as the agent
    python tools/measure_answers.py --k 6
    python tools/measure_answers.py --json out.json
"""
import argparse
import json
import statistics
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app import config, evaluation, retrieval  # noqa: E402
from app.agent import SYSTEM_PROMPT, _serialize_context, retrieve  # noqa: E402
from app.llm import get_primary_llm  # noqa: E402
from app.vectorstore import get_vector_store  # noqa: E402


def measure(k: int = 4) -> dict:
    dataset = json.loads(config.EVAL_DATASET_PATH.read_text(encoding="utf-8"))
    store = get_vector_store()
    model = get_primary_llm()
    rows = []
    for item in dataset:
        query = retrieval.focus_query(item["query"])
        retrieved = retrieve(store, query, k=k)
        prompt = (f"CONTEXT:\n{_serialize_context(retrieved)}\n\nTOOL OBSERVATIONS:\n\n"
                  f"QUESTION:\n{query}")
        answer = model.generate(SYSTEM_PROMPT, prompt)
        metrics = evaluation.evaluate_response(item["query"], answer, retrieved,
                                               expected_keywords=item.get("expected_keywords"))
        docs = {r["source"].replace(".txt", "") for r in retrieved}
        expected = set(item.get("expected_topics", []))
        lower = answer.lower()
        rows.append({
            "id": item["id"], "query": item["query"], **metrics,
            "any_doc": bool(docs & expected), "all_docs": expected <= docs,
            "missing_keys": [key for key in item.get("expected_keywords", [])
                             if key.lower() not in lower
                             and not (evaluation._numeric_atoms(key)
                                      and evaluation._numeric_atoms(key)
                                      <= evaluation._numeric_atoms(answer))],
            "multi_doc": len(expected) > 1,
        })
    mean = lambda key, rs=rows: round(statistics.mean(r[key] for r in rs), 3)  # noqa: E731
    multi = [r for r in rows if r["multi_doc"]]
    return {
        "k": k,
        "context_relevance": mean("context_relevance"),
        "groundedness": mean("groundedness"),
        "answer_relevance": mean("answer_relevance"),
        "answer_relevance_multi_doc": mean("answer_relevance", multi) if multi else None,
        "any_at_k": round(sum(r["any_doc"] for r in rows) / len(rows), 3),
        "all_at_k": round(sum(r["all_docs"] for r in rows) / len(rows), 3),
        "rows": rows,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--k", type=int, default=4)
    parser.add_argument("--json", default="")
    args = parser.parse_args()
    result = measure(args.k)
    for key in ("k", "context_relevance", "groundedness", "answer_relevance",
                "answer_relevance_multi_doc", "any_at_k", "all_at_k"):
        print(f"{key:<28}{result[key]}")
    print("\nQuestions missing gold keys:")
    for r in result["rows"]:
        if r["missing_keys"]:
            tag = "multi" if r["multi_doc"] else "single"
            print(f"  {r['id']:<4} {tag:<6} ans={r['answer_relevance']:.2f} "
                  f"all_docs={'y' if r['all_docs'] else 'n'}  missing={r['missing_keys']}")
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
