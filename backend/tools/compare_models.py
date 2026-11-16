"""
Live model comparison: GPT-4o-mini against an open-source model, over the
identical retrieval, prompt and evaluation set the agent uses.

Every model that is reachable is run; the offline composer is always run too,
as the baseline and as the fallback the agent uses when a model is down.

For each model it reports:
  * the three RAG-Triad metrics (context relevance is the same for all, since
    retrieval does not depend on the model)
  * how many answers the safety check would have withheld for stating a
    figure absent from the context - in production those are replaced by the
    offline composer's answer
  * how many questions have every gold answer key in the answer
  * errors and median / worst response time

It then recommends a model for CHAT_MODEL_PROVIDER: among live models whose
groundedness reaches the release gate (0.95), the highest groundedness, then
fewest withheld answers, then answer relevance. If none reaches it, the
offline composer stays.

    python tools/compare_models.py                 # every reachable model
    python tools/compare_models.py --only ollama   # one provider plus the baseline
    python tools/compare_models.py --limit 5       # quick smoke run

Setup:
  OpenAI   set OPENAI_API_KEY in .env
  Ollama   install from ollama.com, then:  ollama pull llama3.2:3b
           (OPEN_SOURCE_MODEL_NAME must match the pulled tag)

Results are written to tools/model_comparison.json.
"""
import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import requests  # noqa: E402

from app import config, evaluation, retrieval  # noqa: E402
from app.agent import SYSTEM_PROMPT, _serialize_context, retrieve  # noqa: E402
from app.llm import (DeterministicExtractiveLLM, OllamaLLM, OpenAILLM,  # noqa: E402
                     check_live_answer)
from app.vectorstore import get_vector_store  # noqa: E402

OUT = Path(__file__).resolve().parent / "model_comparison.json"
GATE_GROUNDEDNESS = 0.95   # the release quality gate's threshold


def missing_keys(item: dict, answer: str) -> list:
    lower = answer.lower()
    return [key for key in item.get("expected_keywords", [])
            if key.lower() not in lower
            and not (evaluation._numeric_atoms(key)
                     and evaluation._numeric_atoms(key) <= evaluation._numeric_atoms(answer))]


def _ollama_ready() -> str:
    """'' when the configured model is pulled and served, else the reason."""
    try:
        tags = requests.get(f"{config.OLLAMA_BASE_URL}/api/tags", timeout=3).json()
    except Exception as exc:  # noqa: BLE001
        return f"Ollama not reachable at {config.OLLAMA_BASE_URL} ({type(exc).__name__})"
    names = {m.get("name") for m in tags.get("models", [])}
    wanted = config.OPEN_SOURCE_MODEL_NAME
    if wanted not in names and f"{wanted}:latest" not in names:
        return (f"model '{wanted}' is not pulled (have: {', '.join(sorted(names)) or 'none'}); "
                f"run: ollama pull {wanted}")
    return ""


def candidates(only: str = ""):
    models, skipped = [], {}
    if only in ("", "openai"):
        if config.OPENAI_API_KEY:
            models.append(("openai", OpenAILLM()))
        else:
            skipped["openai"] = "OPENAI_API_KEY is not set"
    if only in ("", "ollama"):
        reason = _ollama_ready()
        if reason:
            skipped["ollama"] = reason
        else:
            models.append(("ollama", OllamaLLM()))
    models.append(("offline", DeterministicExtractiveLLM(
        variant="primary", name="extractive-broad (offline, not an LLM)")))
    return models, skipped


def run(only: str = "", limit: int = 0, out: Path = OUT) -> dict:
    dataset = json.loads(config.EVAL_DATASET_PATH.read_text(encoding="utf-8"))
    if limit:
        dataset = dataset[:limit]
    store = get_vector_store()
    models, skipped = candidates(only)
    for key, reason in skipped.items():
        print(f"  skipped {key}: {reason}")

    rows = []
    per_model = {key: {"metrics": [], "times": [], "withheld": 0, "errors": 0, "all_keys": 0}
                 for key, _ in models}
    for n, item in enumerate(dataset, 1):
        query = retrieval.focus_query(item["query"])
        retrieved = retrieve(store, query)
        user_prompt = (f"CONTEXT:\n{_serialize_context(retrieved)}\n\nTOOL OBSERVATIONS:\n\n"
                       f"QUESTION:\n{query}")
        row = {"id": item["id"], "query": item["query"], "models": {}}
        for key, llm in models:
            started = time.perf_counter()
            try:
                answer, error = llm.generate(SYSTEM_PROMPT, user_prompt), None
            except Exception as exc:  # noqa: BLE001
                answer, error = "", f"{type(exc).__name__}: {exc}"[:300]
            elapsed = time.perf_counter() - started
            stats = per_model[key]
            stats["times"].append(elapsed)
            if error:
                stats["errors"] += 1
            withheld = None if llm.is_simulated or error else check_live_answer(answer, user_prompt)
            if withheld:
                stats["withheld"] += 1
            metrics = evaluation.evaluate_response(item["query"], answer, retrieved,
                                                   expected_keywords=item.get("expected_keywords"))
            stats["metrics"].append(metrics)
            missing = missing_keys(item, answer)
            if not missing and not error:
                stats["all_keys"] += 1
            row["models"][key] = {"answer": answer, "error": error, "withheld": withheld,
                                  "seconds": round(elapsed, 2), "missing_keys": missing, **metrics}
        rows.append(row)
        print(f"  [{n}/{len(dataset)}] {item['id']}", flush=True)

    summary = {}
    for key, llm in models:
        stats = per_model[key]
        m = stats["metrics"]
        summary[key] = {
            "model": llm.name,
            "is_live_model": not llm.is_simulated,
            "context_relevance": round(statistics.mean(x["context_relevance"] for x in m), 3),
            "groundedness": round(statistics.mean(x["groundedness"] for x in m), 3),
            "answer_relevance": round(statistics.mean(x["answer_relevance"] for x in m), 3),
            "withheld_answers": stats["withheld"],
            "questions_with_every_key": stats["all_keys"],
            "errors": stats["errors"],
            "median_seconds": round(statistics.median(stats["times"]), 2),
            "max_seconds": round(max(stats["times"]), 2),
        }

    live = [k for k, v in summary.items() if v["is_live_model"] and v["errors"] < len(dataset)]
    passing = [k for k in live if summary[k]["groundedness"] >= GATE_GROUNDEDNESS]
    if passing:
        best = max(passing, key=lambda k: (summary[k]["groundedness"], -summary[k]["withheld_answers"],
                                        summary[k]["answer_relevance"]))
        recommendation = {"CHAT_MODEL_PROVIDER": best, "model": summary[best]["model"]}
    else:
        recommendation = {"CHAT_MODEL_PROVIDER": "offline",
                          "model": summary["offline"]["model"],
                          "note": ("No live model reached groundedness "
                                   f"{GATE_GROUNDEDNESS}." if live else
                                   "No live model was reachable; nothing was compared.")}

    result = {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "questions": len(dataset),
        "metric_version": 2,
        "is_model_comparison": len(live) >= 2,
        "skipped": skipped,
        "summary": summary,
        "recommendation": recommendation,
        "results": rows,
    }
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", choices=["openai", "ollama"], default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--out", default=str(OUT), help="where to write the JSON results")
    args = parser.parse_args()
    result = run(args.only, args.limit, Path(args.out))

    print()
    header = f"{'model':<42}{'ctx':>7}{'grnd':>7}{'ans':>7}{'keys':>6}{'held':>6}{'err':>5}{'p50 s':>8}"
    print(header)
    print("-" * len(header))
    for s in result["summary"].values():
        print(f"{s['model'][:41]:<42}{s['context_relevance']:>7.3f}{s['groundedness']:>7.3f}"
              f"{s['answer_relevance']:>7.3f}{s['questions_with_every_key']:>6}"
              f"{s['withheld_answers']:>6}{s['errors']:>5}"
              f"{s['median_seconds']:>8.2f}")
    print()
    if not result["is_model_comparison"]:
        print("Fewer than two live models ran, so this is not yet the two-model comparison.")
    rec = result["recommendation"]
    print(f"Recommended: CHAT_MODEL_PROVIDER={rec['CHAT_MODEL_PROVIDER']}  ({rec['model']})")
    print(f"Full results: {args.out}")


if __name__ == "__main__":
    main()
