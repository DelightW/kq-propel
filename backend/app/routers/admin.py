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
from pydantic import BaseModel, Field

from app import auth, config, database, handover, payments, sentiment
from app.llm import compare_models, comparison_mode
from app.vectorstore import get_vector_store
from app import retrieval
from app.agent import SYSTEM_PROMPT, _serialize_context, retrieve

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

    if evaluations:
        avg_context = round(statistics.mean(e["context_relevance"] for e in evaluations), 3)
        avg_grounded = round(statistics.mean(e["groundedness"] for e in evaluations), 3)
        avg_relevance = round(statistics.mean(e["answer_relevance"] for e in evaluations), 3)
        hallucination_rate = round(1 - avg_grounded, 3)
    else:
        avg_context = avg_grounded = avg_relevance = hallucination_rate = 0.0

    # Settled value is paid transactions only, per currency. It used to add up
    # every row - failed, pending and simulated attempts included - so a failed
    # Ksh 5,000 push and a pending Ksh 9,000 one were reported as Ksh 14,000
    # received. Pending money is shown separately and never as income.
    payments.reconcile_pending()
    ledger = payments.summary(limit_recent=10)

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
            "count": ledger["count"],
            "settled_by_currency": {k: round(v, 2) for k, v in ledger["settled_by_currency"].items()},
            "pending_by_currency": {k: round(v, 2) for k, v in ledger["pending_by_currency"].items()},
            "counts_by_status": ledger["counts_by_status"],
            "counts_by_method": ledger["counts_by_method"],
            "recent": ledger["recent"],
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
        # Exactly what the agent gives a model: the focused request, the same
        # number of sections, and the [Source | Section] labels it cites from.
        query = retrieval.focus_query(item["query"])
        retrieved = retrieve(store, query)
        user_prompt = (f"CONTEXT:\n{_serialize_context(retrieved)}\n\nTOOL OBSERVATIONS:\n\n"
                       f"QUESTION:\n{query}")
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


# ---------------------------------------------------------------------------
# Human handover queue
# ---------------------------------------------------------------------------

class HandoverReply(BaseModel):
    message: str = Field(min_length=1, max_length=config.MAX_MESSAGE_CHARS)


class HandoverClose(BaseModel):
    note: Optional[str] = Field(default=None, max_length=config.MAX_MESSAGE_CHARS)


@router.get("/handovers")
def list_handovers(include_closed: bool = False,
                   session: Dict = Depends(auth.require_staff)):
    """Passengers waiting for a person, oldest-open first."""
    return {"enabled": config.HANDOVER_ENABLED,
            "cases": handover.list_cases(include_closed=include_closed)}


@router.get("/handovers/{case_id}")
def get_handover(case_id: int, request: Request, session: Dict = Depends(auth.require_staff)):
    """One case with the passenger's conversation. Reading a transcript is
    recorded in the audit log, as it may contain personal details."""
    case = handover.get(case_id)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such case.")
    database.record_admin_audit(session.get("username"), "view_handover",
                                handover.reference(case), auth.client_ip(request))
    return {**case, "reference": handover.reference(case),
            "transcript": handover.transcript(case["session_id"], limit=200)}


@router.post("/handovers/{case_id}/reply")
def reply_handover(case_id: int, body: HandoverReply, request: Request,
                   session: Dict = Depends(auth.require_staff)):
    try:
        case = handover.staff_reply(case_id, session.get("username"), body.message)
    except LookupError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such case.")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    database.record_admin_audit(session.get("username"), "reply_handover",
                                handover.reference(case), auth.client_ip(request))
    return {**case, "reference": handover.reference(case)}


@router.post("/handovers/{case_id}/close")
def close_handover(case_id: int, body: HandoverClose, request: Request,
                   session: Dict = Depends(auth.require_staff)):
    try:
        case = handover.close(case_id, session.get("username"), body.note)
    except LookupError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such case.")
    database.record_admin_audit(session.get("username"), "close_handover",
                                handover.reference(case), auth.client_ip(request))
    return {**case, "reference": handover.reference(case)}
