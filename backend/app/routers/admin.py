"""
Administrative Dashboard API: sentiment metrics, hallucination/groundedness
telemetry, RAG-Triad scores, transaction audit trail, and the dual-model
comparison introduced in the corrections document.

Every data route requires an authenticated staff session. These endpoints
expose the transaction ledger, which stores passenger phone numbers, so
anonymous access was a disclosure of personal data, not merely an untidy
default.
"""
import json
import statistics
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from app import auth, config, database, sentiment
from app.llm import compare_models, comparison_mode
from app.vectorstore import get_vector_store
from app.agent import SYSTEM_PROMPT

router = APIRouter(prefix="/api/admin", tags=["admin"])


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(req: LoginRequest, request: Request, response: Response):
    """Authenticates a staff member and issues a session cookie.

    Failures are deliberately indistinguishable from one another: a wrong
    username and a wrong password return the same message, so this endpoint
    cannot be used to enumerate valid accounts.
    """
    ip = auth.client_ip(request)
    username = (req.username or "").strip()

    locked = auth.lockout_remaining(username, ip)
    if locked:
        database.record_admin_audit(username, "login_blocked",
                                     f"locked for {locked}s", ip)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed attempts. Try again in {locked} seconds.",
        )

    cred = auth.credential()
    username_ok = username.lower() == cred.username.lower()
    # Verify the password even when the username is wrong, so the response
    # time does not reveal which half of the credential was incorrect.
    password_ok = auth.verify_password(req.password or "", cred.password_hash)

    if not (username_ok and password_ok):
        auth.record_failure(username, ip)
        database.record_admin_audit(username, "login_failed", None, ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                             detail="Invalid username or password.")

    auth.clear_failures(username, ip)
    token = auth.issue_staff_session(
        cred.username, ip, request.headers.get("user-agent", ""))
    response.set_cookie(
        auth.ADMIN_COOKIE, token,
        **auth.cookie_kwargs(max_age=config.ADMIN_SESSION_TTL_MINUTES * 60),
    )
    database.record_admin_audit(cred.username, "login_success", None, ip)
    return {"authenticated": True, "username": cred.username,
            "expires_in_minutes": config.ADMIN_SESSION_TTL_MINUTES}


@router.post("/logout")
def logout(request: Request, response: Response,
            session: Optional[Dict] = Depends(auth.optional_staff)):
    token = request.cookies.get(auth.ADMIN_COOKIE)
    if token:
        auth.revoke_staff_session(token)
    if session:
        database.record_admin_audit(session.get("username"), "logout", None,
                                     auth.client_ip(request))
    response.delete_cookie(auth.ADMIN_COOKIE, path="/")
    return {"authenticated": False}


@router.get("/session")
def session_status(session: Optional[Dict] = Depends(auth.optional_staff)):
    """Unauthenticated probe so the dashboard can decide what to render.

    It reports whether a session exists and nothing else - no metrics, no
    passenger data - so it is safe to leave open.
    """
    if not session:
        return {"authenticated": False}
    return {"authenticated": True, "username": session.get("username"),
            "last_seen_at": session.get("last_seen_at")}


@router.get("/overview")
def overview(request: Request, session: Dict = Depends(auth.require_staff)):
    vector_stats = get_vector_store().stats()
    database.record_admin_audit(session.get("username"), "view_overview", None,
                                 auth.client_ip(request))
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

    # Transactions can now settle on two rails in two currencies, so a single
    # total would silently add dollars to shillings. Totals are kept per
    # currency and the shilling figure is reported separately.
    totals_by_currency: Dict[str, float] = {}
    for t in transactions:
        code = (t.get("currency") or "KES").upper()
        totals_by_currency[code] = totals_by_currency.get(code, 0.0) + (t["amount"] or 0)
    total_amount = totals_by_currency.get("KES", 0.0)
    counts_by_method: Dict[str, int] = {}
    for t in transactions:
        rail = (t.get("method") or "mpesa").lower()
        counts_by_method[rail] = counts_by_method.get(rail, 0) + 1

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
            "totals_by_currency": {k: round(v, 2) for k, v in totals_by_currency.items()},
            "counts_by_method": counts_by_method,
            "recent": transactions[:10],
        },
        "sentiment_model_metrics": sentiment.get_metrics(),
    }


@router.get("/transactions")
def transactions(request: Request, session: Dict = Depends(auth.require_staff)):
    database.record_admin_audit(session.get("username"), "view_transactions",
                                 None, auth.client_ip(request))
    return database.fetch_transactions(limit=100)


@router.get("/audit")
def audit(session: Dict = Depends(auth.require_staff)):
    """Who accessed passenger data, and when."""
    return {"entries": database.fetch_admin_audit(limit=100)}


@router.get("/model-comparison")
def model_comparison(request: Request, session: Dict = Depends(auth.require_staff)):
    """Runs both configured generators across the full evaluation dataset over
    identical RAG context.

    The response is self-describing: `comparison` states whether this run is a
    genuine dual-model comparison or - when no LLM endpoint is reachable - a
    response-breadth ablation of one deterministic composer. Both columns share
    a code path in the offline case and must not be presented as a model study.
    """
    dataset = json.loads(config.EVAL_DATASET_PATH.read_text(encoding="utf-8"))
    database.record_admin_audit(session.get("username"), "run_model_comparison",
                                 f"{len(dataset)} queries", auth.client_ip(request))
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
