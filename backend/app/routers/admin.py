"""
Administrative Dashboard API: sentiment metrics, hallucination/groundedness
telemetry, RAG-Triad scores, transaction audit trail, and the dual-model
comparison introduced in the corrections document.
"""
import json
import statistics
from fastapi import APIRouter

from app import config, database, sentiment
from app.llm import compare_models
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
    """Runs both LLMs (primary GPT-4o-mini configuration vs. the open-source
    alternate model) across the full evaluation dataset, over the identical
    RAG context, per the corrections document's controlled comparison
    requirement."""
    dataset = json.loads(config.EVAL_DATASET_PATH.read_text(encoding="utf-8"))
    store = get_vector_store()
    results = []
    for item in dataset:
        retrieved = store.similarity_search(item["query"], k=3)
        context_text = "\n---\n".join(r["text"] for r in retrieved)
        user_prompt = f"CONTEXT:\n{context_text}\n\nTOOL OBSERVATIONS:\n\nQUESTION:\n{item['query']}"
        model_outputs = compare_models(SYSTEM_PROMPT, user_prompt)
        from app import evaluation
        row = {"query_id": item["id"], "query": item["query"], "models": []}
        for out in model_outputs:
            metrics = evaluation.evaluate_response(item["query"], out["response"], retrieved)
            row["models"].append({**out, **metrics})
        results.append(row)
    return {"evaluation_dataset_size": len(dataset), "results": results}
