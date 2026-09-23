"""
Administrative Dashboard API: sentiment metrics, hallucination/groundedness
telemetry, RAG-Triad scores, transaction audit trail, and the dual-model
comparison introduced in the corrections document.
"""
import json
import statistics
from fastapi import APIRouter

from app import config, database, sentiment
from app.llm import compare_models, comparison_mode
from app.vectorstore import get_vector_store
from app.agent import SYSTEM_PROMPT

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/overview")
def overview():
    vector_stats = get_vector_store().stats()
    sentiment_dist = database.fetch_sentiment_distribution()
    evaluations = database.fetch_rag_evaluations(limit=200)
    transactions = database.fetch_transactions(limit=200)

    if evaluations:
        avg_context = round(statistics.mean(e["context_relevance"] for e in evaluations), 3)
        avg_grounded = round(statistics.mean(e["groundedness"] for e in evaluations), 3)
        avg_relevance = round(statistics.mean(e["answer_relevance"] for e in evaluations), 3)
        hallucination_rate = round(1 - avg_grounded, 3)
    else:
        avg_context = avg_grounded = avg_relevance = hallucination_rate = 0.0

    total_amount = sum(t["amount"] or 0 for t in transactions)

    return {
        "app_title": config.APP_TITLE,
        "vector_store": vector_stats,
        "sentiment_distribution": sentiment_dist,
        "rag_triad": {
            "context_relevance": avg_context,
            "groundedness": avg_grounded,
            "answer_relevance": avg_relevance,
            "hallucination_rate": hallucination_rate,
            "sample_size": len(evaluations),
            "metric_version": database.METRIC_VERSION,
        },
        "transactions": {
            "count": len(transactions),
            "total_amount_ksh": total_amount,
            "recent": transactions[:10],
        },
        "sentiment_model_metrics": sentiment.get_metrics(),
    }


@router.get("/transactions")
def transactions():
    return database.fetch_transactions(limit=100)


@router.get("/model-comparison")
def model_comparison():
    """Runs both configured generators across the full evaluation dataset over
    identical RAG context.

    The response is self-describing: `comparison` states whether this run is a
    genuine dual-model comparison or - when no LLM endpoint is reachable - a
    response-breadth ablation of one deterministic composer. Both columns share
    a code path in the offline case and must not be presented as a model study.
    """
    dataset = json.loads(config.EVAL_DATASET_PATH.read_text(encoding="utf-8"))
    store = get_vector_store()
    mode = comparison_mode()
    results = []
    for item in dataset:
        retrieved = store.similarity_search(item["query"], k=3)
        context_text = "\n---\n".join(r["text"] for r in retrieved)
        user_prompt = f"CONTEXT:\n{context_text}\n\nTOOL OBSERVATIONS:\n\nQUESTION:\n{item['query']}"
        model_outputs = compare_models(SYSTEM_PROMPT, user_prompt)
        from app import evaluation
        row = {"query_id": item["id"], "query": item["query"], "models": []}
        for out in model_outputs:
            metrics = evaluation.evaluate_response(
                item["query"], out["response"], retrieved,
                expected_keywords=item.get("expected_keywords"),
            )
            row["models"].append({**out, **metrics})
        # Whether the two generators actually diverged on this query.
        responses = [m["response"] for m in row["models"]]
        row["responses_identical"] = len(set(responses)) == 1
        results.append(row)

    identical = sum(1 for r in results if r["responses_identical"])
    return {
        "evaluation_dataset_size": len(dataset),
        "comparison": mode,
        "metric_version": database.METRIC_VERSION,
        "identical_response_count": identical,
        "identical_response_rate": round(identical / len(results), 3) if results else 0.0,
        "results": results,
    }
