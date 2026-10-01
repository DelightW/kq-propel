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

from app import auth, config, database
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
    if not auth.chat_session_is_valid(kq_session):
        session_id = auth.issue_chat_session(auth.client_ip(request))
        response.set_cookie(auth.CHAT_COOKIE, session_id,
                             **auth.cookie_kwargs(max_age=60 * 60 * 12))
    else:
        session_id = kq_session

    if req.session_id and req.session_id != session_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Session mismatch. Reload the page to start a new session.",
        )

    result = run_agent_turn(session_id, message)
    result["session_id"] = session_id
    return result


@router.post("/reset")
def reset_session(request: Request, response: Response,
                   kq_session: Optional[str] = Cookie(default=None)):
    """Abandons the current session and issues a new one.

    Any pending payment belonging to the old session is cleared, so a shared
    device cannot leave a part-finished transaction available to the next
    person to use it.
    """
    if kq_session:
        database.clear_pending_payment(kq_session)
    session_id = auth.issue_chat_session(auth.client_ip(request))
    response.set_cookie(auth.CHAT_COOKIE, session_id,
                         **auth.cookie_kwargs(max_age=60 * 60 * 12))
    return {"session_id": session_id, "reset": True}
