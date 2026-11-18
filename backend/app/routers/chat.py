"""
Passenger chat API.

Session identifiers are issued by the server and bound to an HttpOnly cookie.
Previously the browser invented its own identifier and the server accepted it,
which meant conversation history and - more seriously - pending payment state
could be addressed by anyone who supplied another passenger's string.
"""
from typing import Optional, Tuple

from fastapi import APIRouter, Cookie, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app import (auth, config, database, handover, memory, payments, privacy, ratelimit, receipts,
                 service_requests)
from app.agent import run_agent_turn

router = APIRouter(prefix="/api/chat", tags=["chat"])


def _owner(response: Response, kq_owner: Optional[str]) -> str:
    """The digest of this browser's owner cookie, issuing the cookie if the
    browser has none. It groups the browser's chats into a list."""
    digest = auth.owner_digest(kq_owner)
    if digest is None:
        kq_owner = auth.new_owner_token()
        digest = auth.owner_digest(kq_owner)
    # Refreshed on use, so the list lasts while the browser keeps coming back.
    response.set_cookie(auth.OWNER_COOKIE, kq_owner, **auth.cookie_kwargs(max_age=auth.OWNER_MAX_AGE))
    return digest


def _issue(request: Request, response: Response, owner: str) -> str:
    session_id = auth.issue_chat_session(auth.client_ip(request), owner)
    response.set_cookie(auth.CHAT_COOKIE, session_id, **auth.cookie_kwargs(max_age=60 * 60 * 12))
    return session_id


class ChatRequest(BaseModel):
    # Bounded at the schema level so an oversized payload is rejected before
    # it reaches the classifier or the retriever.
    message: str = Field(min_length=1, max_length=config.MAX_MESSAGE_CHARS)
    session_id: Optional[str] = None


@router.post("/session")
def start_session(request: Request, response: Response,
                   kq_session: Optional[str] = Cookie(default=None),
                   kq_owner: Optional[str] = Cookie(default=None)):
    """Issues or confirms a passenger session.

    An existing valid cookie is reused so a page reload keeps the same
    conversation; anything else is replaced with a freshly issued identifier.
    """
    owner = _owner(response, kq_owner)
    if auth.chat_session_is_valid(kq_session):
        database.claim_chat_session(kq_session, owner)
        return {"session_id": kq_session, "resumed": True}

    ratelimit.check_http("session_ip", auth.client_ip(request))
    session_id = _issue(request, response, owner)
    return {"session_id": session_id, "resumed": False}


@router.post("")
def chat(req: ChatRequest, request: Request, response: Response,
          kq_session: Optional[str] = Cookie(default=None),
          kq_owner: Optional[str] = Cookie(default=None)):
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
        session_id = _issue(request, response, _owner(response, kq_owner))
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
    waiting = service_requests.waiting_for_staff(kq_session)
    return {
        "handover": handover.status_for_passenger(kq_session),
        # A paid name correction staff have not finished: the chat keeps
        # polling so the "done" message appears without a reload.
        "request": service_requests.reference(waiting) if waiting else None,
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
              kq_session: Optional[str] = Cookie(default=None),
              kq_owner: Optional[str] = Cookie(default=None)):
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
    session_id = _issue(request, response, _owner(response, kq_owner))
    return {"erased": counts, "session_id": session_id}


@router.get("/privacy")
def privacy_notice_settings():
    """The retention periods and contact the privacy notice shows, so the
    notice can never state a period the server does not actually apply."""
    return {**privacy.retention_policy(), "contact": config.PRIVACY_CONTACT,
            "memory_minutes": config.CONVERSATION_MEMORY_MINUTES}


@router.post("/reset")
def reset_session(request: Request, response: Response,
                   kq_session: Optional[str] = Cookie(default=None),
                   kq_owner: Optional[str] = Cookie(default=None)):
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
    session_id = _issue(request, response, _owner(response, kq_owner))
    return {"session_id": session_id, "reset": True}


# ------------------------------------------------------------------ chat list
#
# A browser's own chats, as in other chat assistants: open an earlier chat,
# start a new one, pin or delete one. Threads are addressed by a public thread
# id and only within the caller's owner cookie, so one browser can never open,
# pin or delete another's chat. Deleting a chat erases it as "Delete my
# conversation" does.

def _thread(response: Response, kq_owner: Optional[str], thread_id: str) -> Tuple[str, str]:
    owner = _owner(response, kq_owner)
    session_id = database.thread_session(owner, thread_id)
    if not session_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such chat.")
    return owner, session_id


@router.get("/threads")
def list_threads(response: Response, kq_session: Optional[str] = Cookie(default=None),
                 kq_owner: Optional[str] = Cookie(default=None)):
    owner = _owner(response, kq_owner)
    ratelimit.check_http("poll_session", owner)
    current = database.get_chat_session(kq_session) if kq_session else None
    active = current["thread_id"] if current and current.get("owner") == owner else None
    return {"active": active, "threads": [
        {"id": t["thread_id"], "title": (t["title"] or "New chat")[:80], "pinned": bool(t["pinned"]),
         "last_at": t["last_at"]} for t in database.list_threads(owner)]}


@router.post("/threads/new")
def new_thread(request: Request, response: Response,
               kq_session: Optional[str] = Cookie(default=None),
               kq_owner: Optional[str] = Cookie(default=None)):
    """Starts a new chat. The current one stays in the list; an empty current
    chat is reused rather than leaving empty chats behind."""
    owner = _owner(response, kq_owner)
    current = database.get_chat_session(kq_session) if kq_session else None
    if current and current.get("owner") == owner and not database.fetch_conversation(kq_session):
        return {"session_id": kq_session, "thread_id": current["thread_id"]}
    ratelimit.check_http("session_ip", auth.client_ip(request))
    session_id = _issue(request, response, owner)
    return {"session_id": session_id, "thread_id": database.get_chat_session(session_id)["thread_id"]}


@router.post("/threads/{thread_id}/open")
def open_thread(thread_id: str, response: Response, kq_owner: Optional[str] = Cookie(default=None)):
    _, session_id = _thread(response, kq_owner, thread_id)
    ratelimit.check_http("poll_session", session_id)
    response.set_cookie(auth.CHAT_COOKIE, session_id, **auth.cookie_kwargs(max_age=60 * 60 * 12))
    return {"session_id": session_id, "thread_id": thread_id}


class PinRequest(BaseModel):
    pinned: bool


@router.post("/threads/{thread_id}/pin")
def pin_thread(thread_id: str, req: PinRequest, response: Response,
               kq_owner: Optional[str] = Cookie(default=None)):
    _, session_id = _thread(response, kq_owner, thread_id)
    ratelimit.check_http("chat_session", session_id)
    database.set_thread_pinned(session_id, req.pinned)
    return {"thread_id": thread_id, "pinned": req.pinned}


@router.post("/threads/{thread_id}/delete")
def delete_thread(thread_id: str, req: ForgetRequest, request: Request, response: Response,
                  kq_session: Optional[str] = Cookie(default=None),
                  kq_owner: Optional[str] = Cookie(default=None)):
    """Erases one chat and everything linked to it (see app/privacy.py). If it
    was the open chat, a new empty one is started."""
    if not req.confirm:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Deletion must be confirmed.")
    owner, session_id = _thread(response, kq_owner, thread_id)
    ratelimit.check_http("session_ip", auth.client_ip(request))
    counts = privacy.erase_sessions([session_id])
    current = kq_session
    if kq_session == session_id or not auth.chat_session_is_valid(kq_session):
        current = _issue(request, response, owner)
    return {"erased": counts, "session_id": current,
            "thread_id": database.get_chat_session(current)["thread_id"]}
