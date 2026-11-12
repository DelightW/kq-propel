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
from datetime import datetime
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app import (auth, config, database, handover, payments, privacy, receipts, review, sentiment,
                 service_requests, staff)
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

    # The password is checked even for an unknown or disabled username, so the
    # response time does not reveal which half of the credential was wrong.
    account = staff.authenticate(username, req.password or "")
    if not account:
        auth.record_failure(username, ip)
        # The forwarded chain is kept with a failed sign-in so an attack can be
        # traced through the proxies (and TRUSTED_PROXY_HOPS checked against it).
        forwarded = request.headers.get("x-forwarded-for")
        database.record_admin_audit(username, "login_failed",
                                    f"forwarded: {forwarded[:200]}" if forwarded else None, ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                             detail="Invalid username or password.")

    auth.clear_failures(username, ip)
    token = auth.issue_staff_session(
        account["username"], ip, request.headers.get("user-agent", ""))
    response.set_cookie(
        auth.ADMIN_COOKIE, token,
        **auth.cookie_kwargs(max_age=config.ADMIN_SESSION_TTL_MINUTES * 60),
    )
    database.record_admin_audit(account["username"], "login_success", account["role"], ip)
    return {**_who(account), "expires_in_minutes": config.ADMIN_SESSION_TTL_MINUTES}


def _who(account: Dict) -> Dict:
    """What the dashboard needs to know about the person signed in."""
    return {"authenticated": True, "username": account["username"],
            "display_name": account.get("display_name"), "role": account["role"],
            "role_label": staff.ROLE_LABELS.get(account["role"], account["role"]),
            "panels": staff.PANELS.get(account["role"], []),
            "must_change_password": bool(account.get("must_change_password"))}


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
    return {**_who(session), "last_seen_at": session.get("last_seen_at")}


@router.get("/overview")
def overview(request: Request, session: Dict = Depends(auth.require_role(*staff.ROLES))):
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
            # Analysts see totals, not individual payments.
            "recent": (privacy.mask_rows(ledger["recent"])
                       if session.get("role") != staff.ANALYST else []),
        },
        "sentiment_model_metrics": sentiment.get_metrics(),
    }


@router.get("/transactions")
def transactions(request: Request, session: Dict = Depends(auth.require_role(staff.ADMIN, staff.AGENT))):
    database.record_admin_audit(session.get("username"), "view_transactions",
                                 None, auth.client_ip(request))
    # Staff see the last digits only; the full number stays with the payment
    # code that needs it to send the STK push.
    return privacy.mask_rows(database.fetch_transactions(limit=100))


@router.get("/transactions/{reference}/receipt")
def transaction_receipt(reference: str, request: Request,
                        session: Dict = Depends(auth.require_role(staff.ADMIN, staff.AGENT))):
    """PDF receipt for a paid transaction, for staff answering a passenger.
    Recorded in the audit log like any other read of payment data."""
    with database._connect() as conn:
        row = conn.execute("SELECT * FROM transactions WHERE reference = ?", (reference,)).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such payment.")
    row = dict(row)
    try:
        pdf = receipts.build(row)
    except receipts.NotReceiptable:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This payment is not paid.")
    database.record_admin_audit(session.get("username"), "download_receipt", reference,
                                auth.client_ip(request))
    return Response(pdf, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{receipts.filename(row)}"',
        "Cache-Control": "no-store"})


@router.get("/audit")
def audit(user: Optional[str] = None, action: Optional[str] = None, page: int = 1, size: int = 50,
          session: Dict = Depends(auth.require_role(staff.ADMIN))):
    """Who accessed passenger data, and when. Paged and filterable."""
    return review.audit(user=user, action=action, page=page, size=size)


@router.get("/audit.csv")
def audit_csv(request: Request, user: Optional[str] = None, action: Optional[str] = None,
              session: Dict = Depends(auth.require_role(staff.ADMIN))):
    data = review.audit(user=user, action=action, limit=review.CSV_MAX_ROWS)
    database.record_admin_audit(session.get("username"), "export_access_log",
                                f"{len(data['entries'])} rows", auth.client_ip(request))
    return _csv_response(review.to_csv(data["entries"], review.AUDIT_CSV_COLUMNS), "access-log")


def _csv_response(text: str, name: str) -> Response:
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M")
    return Response(text, media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="kq-propel-{name}-{stamp}.csv"',
        "Cache-Control": "no-store"})


# ------------------------------------------------------------ payments, paged

@router.get("/ledger")
def ledger(request: Request, status_filter: Optional[str] = None, method: Optional[str] = None,
           q: Optional[str] = None, page: int = 1, size: int = 25,
           session: Dict = Depends(auth.require_role(staff.ADMIN, staff.AGENT))):
    """Every payment, newest first, filtered by status, method or reference,
    with phone numbers and checkout IDs masked."""
    if page == 1:
        database.record_admin_audit(session.get("username"), "view_transactions",
                                    None, auth.client_ip(request))
    return review.ledger(status=status_filter, method=method, q=q, page=page, size=size)


@router.get("/ledger.csv")
def ledger_csv(request: Request, status_filter: Optional[str] = None, method: Optional[str] = None,
               q: Optional[str] = None,
               session: Dict = Depends(auth.require_role(staff.ADMIN, staff.AGENT))):
    data = review.ledger(status=status_filter, method=method, q=q, limit=review.CSV_MAX_ROWS)
    database.record_admin_audit(session.get("username"), "export_payments",
                                f"{len(data['transactions'])} rows", auth.client_ip(request))
    return _csv_response(review.to_csv(data["transactions"], review.LEDGER_CSV_COLUMNS), "payments")


# ------------------------------------------------------- conversation review

@router.get("/conversations")
def conversations(request: Request, flag: Optional[str] = None, q: Optional[str] = None,
                  page: int = 1, size: int = 25,
                  session: Dict = Depends(auth.require_role(staff.ADMIN, staff.AGENT))):
    """Passenger conversations with their worst scores, ratings and
    frustration, for finding the answers that went wrong."""
    if flag and flag not in review.FLAGS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown filter.")
    if page == 1:
        database.record_admin_audit(session.get("username"), "view_conversations", flag,
                                    auth.client_ip(request))
    return review.list_conversations(flag=flag, q=q, page=page, size=size)


@router.get("/conversations/{session_id}")
def conversation(session_id: str, request: Request,
                 session: Dict = Depends(auth.require_role(staff.ADMIN, staff.AGENT))):
    detail = review.conversation_detail(session_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such conversation.")
    database.record_admin_audit(session.get("username"), "view_conversation",
                                review.short_id(session_id), auth.client_ip(request))
    return detail


@router.post("/conversations/{session_id}/erase")
def erase_conversation(session_id: str, request: Request,
                       session: Dict = Depends(auth.require_role(staff.ADMIN))):
    """Erases one conversation, as the data-protection panel does by case
    reference or phone number."""
    if review.conversation_detail(session_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such conversation.")
    counts = privacy.erase_sessions([session_id])
    database.record_admin_audit(session.get("username"), "erase_conversation",
                                review.short_id(session_id), auth.client_ip(request))
    return {"erased": counts}


# ---------------------------------------------------------- trends, policies

@router.get("/trends")
def trends(days: int = 14, session: Dict = Depends(auth.require_role(*staff.ROLES))):
    """Daily conversations, quality, frustration, ratings and handovers.
    Counts and averages only, so every role may see them."""
    return review.trends(days=days)


@router.get("/knowledge")
def knowledge(session: Dict = Depends(auth.require_role(*staff.ROLES))):
    """The policy documents the assistant answers from, as indexed."""
    return review.knowledge_base()


@router.get("/model-comparison")
def model_comparison(request: Request, session: Dict = Depends(auth.require_role(staff.ADMIN, staff.ANALYST))):
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
                   session: Dict = Depends(auth.require_role(staff.ADMIN, staff.AGENT))):
    """Passengers waiting for a person, oldest-open first."""
    return {"enabled": config.HANDOVER_ENABLED,
            "cases": privacy.mask_rows(handover.list_cases(include_closed=include_closed))}


@router.get("/handovers/{case_id}")
def get_handover(case_id: int, request: Request, session: Dict = Depends(auth.require_role(staff.ADMIN, staff.AGENT))):
    """One case with the passenger's conversation. Reading a transcript is
    recorded in the audit log, as it may contain personal details."""
    case = handover.get(case_id)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such case.")
    database.record_admin_audit(session.get("username"), "view_handover",
                                handover.reference(case), auth.client_ip(request))
    return {**case, "reference": handover.reference(case),
            "transcript": privacy.mask_rows(handover.transcript(case["session_id"], limit=200))}


@router.post("/handovers/{case_id}/reply")
def reply_handover(case_id: int, body: HandoverReply, request: Request,
                   session: Dict = Depends(auth.require_role(staff.ADMIN, staff.AGENT))):
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
                   session: Dict = Depends(auth.require_role(staff.ADMIN, staff.AGENT))):
    try:
        case = handover.close(case_id, session.get("username"), body.note)
    except LookupError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such case.")
    database.record_admin_audit(session.get("username"), "close_handover",
                                handover.reference(case), auth.client_ip(request))
    return {**case, "reference": handover.reference(case)}


# ---------------------------------------------------------------------------
# Data protection
# ---------------------------------------------------------------------------

class ErasureRequest(BaseModel):
    """A passenger's erasure request received outside the chat (by phone,
    email or at a desk). Identify them by the case reference of a handover
    they opened, or by the phone number they used."""
    case_reference: Optional[str] = Field(default=None, max_length=20)
    phone: Optional[str] = Field(default=None, max_length=20)
    confirm: bool = False


@router.get("/privacy")
def privacy_status(session: Dict = Depends(auth.require_role(staff.ADMIN))):
    return {"retention": privacy.retention_policy(), "last_purge": privacy.last_purge(),
            "contact": config.PRIVACY_CONTACT}


@router.post("/privacy/erase")
def erase_passenger(body: ErasureRequest, request: Request,
                    session: Dict = Depends(auth.require_role(staff.ADMIN))):
    """Finds the passenger's sessions and, when `confirm` is true, erases them.
    Without `confirm` it only reports what would be erased, so staff can check
    they have the right person first. Both are recorded in the audit log, with
    the phone number masked."""
    sessions = []
    if body.case_reference:
        ref = body.case_reference.strip().upper()
        if not ref.startswith("H-") or not ref[2:].isdigit():
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="Case references look like H-00012.")
        case = handover.get(int(ref[2:]))
        if case:
            sessions.append(case["session_id"])
    if body.phone:
        sessions += privacy.sessions_for_phone(body.phone)
    if not body.case_reference and not body.phone:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Give a case reference or a phone number.")
    who = body.case_reference or privacy.mask_phone(body.phone)
    if not body.confirm:
        database.record_admin_audit(session.get("username"), "preview_erasure",
                                    f"{who}: {len(set(sessions))} session(s)", auth.client_ip(request))
        return {"confirmed": False, "sessions_found": len(set(sessions))}
    counts = privacy.erase_sessions(sessions)
    database.record_admin_audit(session.get("username"), "erase_passenger_data",
                                f"{who}: {counts}", auth.client_ip(request))
    return {"confirmed": True, "erased": counts}


# ---------------------------------------------------------------------------
# Service requests (name corrections)
# ---------------------------------------------------------------------------

@router.get("/requests")
def list_service_requests(request: Request, include_closed: bool = False,
                          session: Dict = Depends(auth.require_role(staff.ADMIN, staff.AGENT))):
    """Paid requests waiting for staff, then those still waiting for payment.
    Recorded in the audit log, because requests hold passenger names."""
    database.record_admin_audit(session.get("username"), "view_service_requests", None,
                                auth.client_ip(request))
    return {"requests": service_requests.list_requests(include_closed=include_closed)}


@router.post("/requests/{request_id}/complete")
def complete_service_request(request_id: int, request: Request,
                             session: Dict = Depends(auth.require_role(staff.ADMIN, staff.AGENT))):
    """Staff confirm they have updated the booking in Kenya Airways' system."""
    try:
        done = service_requests.complete(request_id, session.get("username"))
    except LookupError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such request.")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    database.record_admin_audit(session.get("username"), "complete_service_request",
                                service_requests.reference(done), auth.client_ip(request))
    return {**done, "reference": service_requests.reference(done)}


# ---------------------------------------------------------------------------
# Staff accounts (Priority 5)
# ---------------------------------------------------------------------------

class NewStaff(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    display_name: Optional[str] = Field(default=None, max_length=80)
    role: str


class RoleChange(BaseModel):
    role: str


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(max_length=200)


def _staff_error(exc: Exception):
    if isinstance(exc, LookupError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such member of staff.")
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.get("/staff")
def list_staff(session: Dict = Depends(auth.require_role(staff.ADMIN))):
    return {"staff": staff.list_staff(), "roles": [
        {"role": r, "label": staff.ROLE_LABELS[r], "panels": staff.PANELS[r]} for r in staff.ROLES]}


@router.post("/staff")
def create_staff(body: NewStaff, request: Request,
                 session: Dict = Depends(auth.require_role(staff.ADMIN))):
    """Creates an account. The one-time password is in this response only;
    it is never stored or logged in readable form."""
    try:
        account = staff.create(body.username, body.display_name or "", body.role, session["username"])
    except (staff.StaffError, LookupError) as exc:
        raise _staff_error(exc)
    database.record_admin_audit(session["username"], "create_staff",
                                f"{account['username']} ({account['role']})", auth.client_ip(request))
    return account


@router.post("/staff/{username}/role")
def change_staff_role(username: str, body: RoleChange, request: Request,
                      session: Dict = Depends(auth.require_role(staff.ADMIN))):
    try:
        account = staff.set_role(username, body.role)
    except (staff.StaffError, LookupError) as exc:
        raise _staff_error(exc)
    database.record_admin_audit(session["username"], "change_staff_role",
                                f"{account['username']} -> {account['role']}", auth.client_ip(request))
    return account


@router.post("/staff/{username}/disable")
def disable_staff(username: str, request: Request,
                  session: Dict = Depends(auth.require_role(staff.ADMIN))):
    if username.lower() == session["username"].lower():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="You cannot disable your own account.")
    try:
        account = staff.set_active(username, False)
    except (staff.StaffError, LookupError) as exc:
        raise _staff_error(exc)
    database.record_admin_audit(session["username"], "disable_staff", account["username"],
                                auth.client_ip(request))
    return account


@router.post("/staff/{username}/enable")
def enable_staff(username: str, request: Request,
                 session: Dict = Depends(auth.require_role(staff.ADMIN))):
    try:
        account = staff.set_active(username, True)
    except (staff.StaffError, LookupError) as exc:
        raise _staff_error(exc)
    database.record_admin_audit(session["username"], "enable_staff", account["username"],
                                auth.client_ip(request))
    return account


@router.post("/staff/{username}/reset-password")
def reset_staff_password(username: str, request: Request,
                         session: Dict = Depends(auth.require_role(staff.ADMIN))):
    """Issues a new one-time password and signs the person out everywhere."""
    try:
        account = staff.reset_password(username)
    except (staff.StaffError, LookupError) as exc:
        raise _staff_error(exc)
    database.record_admin_audit(session["username"], "reset_staff_password", account["username"],
                                auth.client_ip(request))
    return account


@router.post("/password")
def change_password(body: PasswordChange, request: Request,
                    session: Dict = Depends(auth.require_staff)):
    """Any member of staff changing their own password (and the only thing a
    person holding a one-time password may do)."""
    try:
        account = staff.change_own_password(session["username"], body.current_password, body.new_password)
    except staff.StaffError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    database.record_admin_audit(session["username"], "change_own_password", None, auth.client_ip(request))
    return _who(account)
