"""
Compares the offline extractive composer with a live model served by Ollama,
over the evaluation set and identical retrieved context.

For every question both generators receive the same prompt the agent builds
(focused question, k=4 sections, [Source | Section] labels). Each answer is
scored with the RAG Triad, checked for the gold answer keys, timed, and - for
the live model - put through the same figure check the agent applies before a
live answer may reach a passenger (llm.check_live_answer).

    ollama pull llama3.2:3b
    set OPEN_SOURCE_MODEL_NAME=llama3.2:3b
    python tools/compare_models.py --json comparison.json

Ollama must be running (http://localhost:11434). The answers are written to
the JSON file so they can be read, not only scored.
"""
import argparse
import json
import statistics
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app import config, evaluation, retrieval  # noqa: E402
from app.agent import SYSTEM_PROMPT, _serialize_context, retrieve  # noqa: E402
from app.llm import OllamaLLM, _offline_primary, check_live_answer  # noqa: E402
from app.vectorstore import get_vector_store  # noqa: E402


def missing_keys(item, answer):
    lower = answer.lower()
    return [key for key in item.get("expected_keywords", [])
            if key.lower() not in lower
            and not (evaluation._numeric_atoms(key)
                     and evaluation._numeric_atoms(key) <= evaluation._numeric_atoms(answer))]


def summarise(rows, name):
    ok = [r[name] for r in rows if not r[name]["error"]]
    times = [o["seconds"] for o in ok]
    mean = lambda key: round(statistics.mean(o[key] for o in ok), 3) if ok else None  # noqa: E731
    out = {
        "answered": len(ok),
        "errors": len(rows) - len(ok),
        "context_relevance": mean("context_relevance"),
        "groundedness": mean("groundedness"),
        "answer_relevance": mean("answer_relevance"),
        "all_keys_found": sum(1 for o in ok if not o["missing_keys"]),
        "median_seconds": round(statistics.median(times), 2) if times else None,
        "max_seconds": round(max(times), 2) if times else None,
    }
    if name == "live":
        out["passed_figure_check"] = sum(1 for o in ok if o["withheld"] is None)
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", default="")
    parser.add_argument("--limit", type=int, default=0, help="only the first N questions")
    args = parser.parse_args()

    dataset = json.loads(config.EVAL_DATASET_PATH.read_text(encoding="utf-8"))
    if args.limit:
        dataset = dataset[:args.limit]
    store = get_vector_store()
    composer, live = _offline_primary(), OllamaLLM()
    rows = []
    for item in dataset:
        query = retrieval.focus_query(item["query"])
        retrieved = retrieve(store, query)
        prompt = (f"CONTEXT:\n{_serialize_context(retrieved)}\n\nTOOL OBSERVATIONS:\n\n"
                  f"QUESTION:\n{query}")
        row = {"id": item["id"], "query": item["query"]}
        for name, model in (("composer", composer), ("live", live)):
            started = time.perf_counter()
            try:
                answer, error = model.generate(SYSTEM_PROMPT, prompt), None
            except Exception as exc:  # timeout or connection failure
                answer, error = "", f"{type(exc).__name__}: {exc}"[:200]
            seconds = round(time.perf_counter() - started, 2)
            metrics = evaluation.evaluate_response(item["query"], answer, retrieved,
                                                   expected_keywords=item.get("expected_keywords"))
            row[name] = {"model": model.name, "answer": answer, "error": error, "seconds": seconds,
                         **metrics, "missing_keys": missing_keys(item, answer) if answer else None}
            if name == "live":
                row[name]["withheld"] = check_live_answer(answer, prompt) if answer else "no answer"
        rows.append(row)
        lv = row["live"]
        print(f"{item['id']:<4} live {lv['seconds']:>6.1f}s  g={lv['groundedness']:.2f} "
              f"a={lv['answer_relevance']:.2f}  keys_missing={lv['missing_keys']}  "
              f"withheld={lv['withheld']}  error={lv['error']}", flush=True)

    result = {"questions": len(rows), "live_model": live.name,
              "composer": summarise(rows, "composer"), "live": summarise(rows, "live"), "rows": rows}
    print("\n" + json.dumps({k: result[k] for k in ("questions", "live_model", "composer", "live")}, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
