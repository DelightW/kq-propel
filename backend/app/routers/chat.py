"""
Passenger chat API.

Session identifiers are issued by the server and bound to an HttpOnly cookie.
Previously the browser invented its own identifier and the server accepted it,
which meant conversation history and - more seriously - pending payment state
could be addressed by anyone who supplied another passenger's string.
"""
from typing import Optional

from fastapi import APIRouter, Cookie, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app import auth, config, database, handover, memory, payments, privacy, ratelimit, receipts
from app.agent import run_agent_turn

router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatRequest(BaseModel):
    # Bounded at the schema level so an oversized payload is rejected before
    # it reaches the classifier or the retriever.
    message: str = Field(min_length=1, max_length=config.MAX_MESSAGE_CHARS)
    session_id: Optional[str] = None


@router.post("/session")
def start_session(request: Request, response: Response,
                   kq_session: Optional[str] = Cookie(default=None)):
    """Issues or confirms a passenger session.

    An existing valid cookie is reused so a page reload keeps the same
    conversation; anything else is replaced with a freshly issued identifier.
    """
    if auth.chat_session_is_valid(kq_session):
        return {"session_id": kq_session, "resumed": True}

    ratelimit.check_http("session_ip", auth.client_ip(request))
    session_id = auth.issue_chat_session(auth.client_ip(request))
    response.set_cookie(auth.CHAT_COOKIE, session_id,
                         **auth.cookie_kwargs(max_age=60 * 60 * 12))
    return {"session_id": session_id, "resumed": False}


@router.post("")
def chat(req: ChatRequest, request: Request, response: Response,
          kq_session: Optional[str] = Cookie(default=None)):
    message = (req.message or "").strip()
    if not message:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                             detail="Please type a question.")

    # The cookie is the only trusted source of identity. A session_id in the
    # body is accepted only when it agrees with it, which keeps the existing
    # client contract working without letting the body override ownership.
    ip = auth.client_ip(request)
    ratelimit.check_http("chat_ip", ip)
    if not auth.chat_session_is_valid(kq_session):
        ratelimit.check_http("session_ip", ip)
        session_id = auth.issue_chat_session(ip)
        response.set_cookie(auth.CHAT_COOKIE, session_id,
                             **auth.cookie_kwargs(max_age=60 * 60 * 12))
    else:
        session_id = kq_session

    if req.session_id and req.session_id != session_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Session mismatch. Reload the page to start a new session.",
        )

    ratelimit.check_http("chat_session", session_id)
    result = run_agent_turn(session_id, message)
    result["session_id"] = session_id
    return result


@router.get("/payments/{reference}")
def payment_status(reference: str, kq_session: Optional[str] = Cookie(default=None)):
    """Current status of a payment this session started.

    Polled by the chat while a payment is pending. The lookup is scoped to the
    caller's session, so one passenger cannot read another's payment, and a
    stale pending payment is reconciled with its provider before answering.
    """
    if not auth.chat_session_is_valid(kq_session):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No session.")
    ratelimit.check_http("poll_session", kq_session)
    row = payments.for_passenger(kq_session, reference)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found.")
    row = payments.reconcile(row)
    return {
        "reference": row["reference"],
        "status": row["status"],
        "final": row["status"] in payments.FINAL,
        "method": row["method"],
        "currency": row["currency"],
        "amount": payments.from_minor(row["amount_minor"]),
        "receipt": row.get("provider_receipt"),
    }


@router.get("/payments/{reference}/receipt")
def payment_receipt(reference: str, kq_session: Optional[str] = Cookie(default=None)):
    """PDF receipt for one of this session's payments, once it is paid."""
    if not auth.chat_session_is_valid(kq_session):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No session.")
    ratelimit.check_http("receipt_session", kq_session)
    row = payments.for_passenger(kq_session, reference)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found.")
    try:
        pdf = receipts.build(row)
    except receipts.NotReceiptable:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="A receipt is available once the payment is confirmed.")
    return Response(pdf, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{receipts.filename(row)}"',
        "Cache-Control": "no-store"})


@router.get("/handover")
def handover_updates(after: int = 0, kq_session: Optional[str] = Cookie(default=None)):
    """Staff replies posted since message `after`, and the case status.

    Polled by the chat while a handover case is open. Scoped to the caller's
    session cookie like every other passenger read.
    """
    if not auth.chat_session_is_valid(kq_session):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No session.")
    ratelimit.check_http("poll_session", kq_session)
    return {
        "handover": handover.status_for_passenger(kq_session),
        "messages": handover.messages_since(kq_session, max(after, 0)),
    }


@router.get("/history")
def conversation_history(kq_session: Optional[str] = Cookie(default=None)):
    """The caller's own conversation, so a reloaded page shows it again.

    Scoped to the session cookie. Only what the passenger saw is returned:
    their messages, the answers (with their own rating) and staff replies.
    """
    if not auth.chat_session_is_valid(kq_session):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No session.")
    ratelimit.check_http("poll_session", kq_session)
    rows = database.fetch_conversation(kq_session)
    return {"messages": [
        {"id": r["id"], "role": r["role"], "message": r["message"], "created_at": r["created_at"],
         "feedback": r.get("feedback")}
        for r in rows if r["role"] in ("user", "assistant", "agent")
    ][-200:]}


class FeedbackRequest(BaseModel):
    message_id: int
    rating: str = Field(pattern="^(up|down)$")
    note: Optional[str] = Field(default=None, max_length=300)


@router.post("/feedback")
def answer_feedback(req: FeedbackRequest, kq_session: Optional[str] = Cookie(default=None)):
    """A passenger's rating of one answer: helpful or not, with an optional
    short note. Only an answer in the caller's own conversation can be rated,
    and rating again replaces the earlier rating."""
    if not auth.chat_session_is_valid(kq_session):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No session.")
    ratelimit.check_http("chat_session", kq_session)
    note = (req.note or "").strip() or None
    with database._connect() as conn:
        changed = conn.execute(
            "UPDATE conversations SET feedback = ?, feedback_note = ?, feedback_at = ? "
            "WHERE id = ? AND session_id = ? AND role = 'assistant'",
            (req.rating, note, database.datetime.utcnow().isoformat(), req.message_id, kq_session),
        ).rowcount
    if not changed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such answer.")
    return {"message_id": req.message_id, "rating": req.rating}


class ForgetRequest(BaseModel):
    # A JSON body (rather than a bare POST) means a cross-site form cannot
    # trigger erasure: the browser must send a CORS preflight first.
    confirm: bool


@router.post("/forget")
def forget_me(req: ForgetRequest, request: Request, response: Response,
              kq_session: Optional[str] = Cookie(default=None)):
    """Erases the caller's conversation and everything linked to it, then
    starts a fresh session. Payment records are kept as financial records but
    lose the phone number (see app/privacy.py)."""
    if not req.confirm:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Erasure must be confirmed.")
    if not auth.chat_session_is_valid(kq_session):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No session.")
    ratelimit.check_http("session_ip", auth.client_ip(request))
    counts = privacy.erase_sessions([kq_session])
    session_id = auth.issue_chat_session(auth.client_ip(request))
    response.set_cookie(auth.CHAT_COOKIE, session_id,
                         **auth.cookie_kwargs(max_age=60 * 60 * 12))
    return {"erased": counts, "session_id": session_id}


@router.get("/privacy")
def privacy_notice_settings():
    """The retention periods and contact the privacy notice shows, so the
    notice can never state a period the server does not actually apply."""
    return {**privacy.retention_policy(), "contact": config.PRIVACY_CONTACT,
            "memory_minutes": config.CONVERSATION_MEMORY_MINUTES}


@router.post("/reset")
def reset_session(request: Request, response: Response,
                   kq_session: Optional[str] = Cookie(default=None)):
    """Abandons the current session and issues a new one.

    Any pending payment belonging to the old session is cleared, so a shared
    device cannot leave a part-finished transaction available to the next
    person to use it.
    """
    ratelimit.check_http("session_ip", auth.client_ip(request))
    if kq_session:
        database.clear_pending_payment(kq_session)
        # The next person on a shared device must not inherit the previous
        # passenger's topic, flight, or an open conversation with staff.
        memory.forget(kq_session)
        handover.withdraw(kq_session)
    session_id = auth.issue_chat_session(auth.client_ip(request))
    response.set_cookie(auth.CHAT_COOKIE, session_id,
                         **auth.cookie_kwargs(max_age=60 * 60 * 12))
    return {"session_id": session_id, "reset": True}
