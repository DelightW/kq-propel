"""
Human handover.

The assistant used to say "I can escalate you to a human support agent - just
say 'escalate'", and nothing happened when the passenger did. A handover is now
a case:

  open     the passenger asked for a person; it is waiting in the staff queue
  claimed  a member of staff has replied and owns the case
  closed   resolved by staff, or withdrawn by the passenger

Staff work the queue from the admin dashboard and reply into the passenger's
own chat window. Replies are stored in `conversations` with role 'agent', so
the transcript of a case is the conversation itself.

If HANDOVER_WEBHOOK_URL is set, every new case is also POSTed there as JSON so
it can be forwarded into the airline's contact-centre system. Whether the
forward succeeded is recorded on the case.

The assistant only promises a person when HANDOVER_ENABLED is true. Otherwise
it gives the contact route in HANDOVER_FALLBACK_CONTACT.
"""
import re
from datetime import datetime
from typing import Dict, List, Optional

import requests

from app import config, database

OPEN, CLAIMED, CLOSED = "open", "claimed", "closed"
ACTIVE = (OPEN, CLAIMED)

# Asking for a person, in English and Swahili.
_REQUEST_RE = re.compile(
    r"\bescalat(?:e|ion)\b"
    r"|\b(?:speak|talk|chat)\s+(?:to|with)\s+(?:a|an|the|your|some)?\s*"
    r"(?:real\s+|live\s+|actual\s+)?(?:human|person|agent|someone|somebody|staff|"
    r"representative|manager|supervisor|customer\s+(?:care|service))\b"
    r"|\b(?:real|live|actual)\s+(?:human|person|agent)\b"
    r"|\bhuman\s+(?:agent|being|support)\b"
    r"|\b(?:get|give|connect|transfer|put)\s+me\s+(?:to|through\s+to|with)\s+"
    r"(?:a|an|the)?\s*(?:human|person|agent|someone|manager|supervisor)\b"
    r"|\bnataka\s+(?:kuongea|kuzungumza)\s+na\s+(?:mtu|binadamu|wakala)\b",
    re.IGNORECASE)
# "Do I need to speak to an agent at the counter?" is a question about the
# airport, not a request for this chat to hand over.
_ABOUT_AIRPORT_STAFF_RE = re.compile(
    r"\b(?:at|in)\s+the\s+(?:airport|counter|desk|check-?in|gate|lounge)\b", re.IGNORECASE)


def is_request(message: str) -> bool:
    return bool(_REQUEST_RE.search(message)) and not _ABOUT_AIRPORT_STAFF_RE.search(message)


# Withdrawing a request and carrying on with the assistant.
CANCEL_RE = re.compile(
    r"^\s*(?:cancel(?:\s+the)?\s+(?:handover|escalation|request)|never\s*mind|"
    r"back\s+to\s+(?:the\s+)?(?:assistant|bot|chat)|i'?ll\s+continue\s+with\s+(?:you|the\s+bot))"
    r"\b[\s!.,?]*$", re.IGNORECASE)

_TRANSCRIPT_LIMIT = 30


def _now() -> str:
    return datetime.utcnow().isoformat()


def _row(row) -> Optional[Dict]:
    return dict(row) if row else None


def active(session_id: str) -> Optional[Dict]:
    """The session's open or claimed case, if any."""
    with database._connect() as conn:
        return _row(conn.execute(
            "SELECT * FROM handovers WHERE session_id = ? AND status IN (?, ?) "
            "ORDER BY id DESC LIMIT 1", (session_id, *ACTIVE)).fetchone())


def get(case_id: int) -> Optional[Dict]:
    with database._connect() as conn:
        return _row(conn.execute("SELECT * FROM handovers WHERE id = ?", (case_id,)).fetchone())


def reference(case: Dict) -> str:
    return f"H-{case['id']:05d}"


def open_case(session_id: str, reason: str, frustration_score: Optional[float] = None) -> Dict:
    """Opens a case, or returns the session's existing one so asking twice
    does not queue the passenger twice."""
    existing = active(session_id)
    if existing:
        return existing
    now = _now()
    with database._connect() as conn:
        cur = conn.execute(
            "INSERT INTO handovers (session_id, status, reason, frustration_score, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, OPEN, (reason or "")[:500], frustration_score, now, now))
        case_id = cur.lastrowid
    case = get(case_id)
    _forward(case)
    return get(case_id)


def _set(case_id: int, **fields) -> None:
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with database._connect() as conn:
        conn.execute(f"UPDATE handovers SET {cols} WHERE id = ?", (*fields.values(), case_id))


def transcript(session_id: str, limit: int = _TRANSCRIPT_LIMIT) -> List[Dict]:
    with database._connect() as conn:
        rows = conn.execute(
            "SELECT id, role, message, sentiment_label, frustration_score, created_at "
            "FROM conversations WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def _forward(case: Dict) -> None:
    """Sends a new case to the contact-centre webhook, if one is configured.
    A failure is recorded, never raised: the case is still in the staff queue."""
    if not config.HANDOVER_WEBHOOK_URL:
        return
    payload = {
        "case_reference": reference(case),
        "created_at": case["created_at"],
        "reason": case["reason"],
        "frustration_score": case["frustration_score"],
        "transcript": [{"role": m["role"], "message": m["message"], "at": m["created_at"]}
                       for m in transcript(case["session_id"])],
    }
    headers = {"Content-Type": "application/json"}
    if config.HANDOVER_WEBHOOK_TOKEN:
        headers["Authorization"] = f"Bearer {config.HANDOVER_WEBHOOK_TOKEN}"
    try:
        resp = requests.post(config.HANDOVER_WEBHOOK_URL, json=payload, headers=headers,
                             timeout=5)
        result = f"HTTP {resp.status_code}"
    except requests.RequestException as exc:
        result = f"failed: {type(exc).__name__}"
    _set(case["id"], forwarded=result[:200])


def list_cases(include_closed: bool = False, limit: int = 50) -> List[Dict]:
    sql = ("SELECT h.*, (SELECT message FROM conversations c WHERE c.session_id = h.session_id "
           "AND c.role = 'user' ORDER BY c.id DESC LIMIT 1) AS last_passenger_message "
           "FROM handovers h")
    params: tuple = ()
    if not include_closed:
        sql += " WHERE h.status IN (?, ?)"
        params = ACTIVE
    sql += " ORDER BY CASE h.status WHEN 'open' THEN 0 WHEN 'claimed' THEN 1 ELSE 2 END, h.id DESC LIMIT ?"
    with database._connect() as conn:
        rows = conn.execute(sql, (*params, limit)).fetchall()
    return [{**dict(r), "reference": reference(dict(r))} for r in rows]


def staff_reply(case_id: int, username: str, text: str) -> Dict:
    """Posts a staff reply into the passenger's chat and claims the case."""
    case = get(case_id)
    if case is None:
        raise LookupError("No such case.")
    if case["status"] == CLOSED:
        raise ValueError("This case is closed.")
    database.log_message(case["session_id"], "agent", text.strip())
    _set(case_id, status=CLAIMED, claimed_by=username)
    return get(case_id)


def close(case_id: int, username: str, note: Optional[str] = None) -> Dict:
    case = get(case_id)
    if case is None:
        raise LookupError("No such case.")
    if case["status"] != CLOSED:
        if note:
            database.log_message(case["session_id"], "agent", note.strip())
        _set(case_id, status=CLOSED, claimed_by=case["claimed_by"] or username)
    return get(case_id)


def withdraw(session_id: str) -> Optional[Dict]:
    """The passenger chose to carry on with the assistant."""
    case = active(session_id)
    if case:
        _set(case["id"], status=CLOSED)
    return case


def messages_since(session_id: str, after_id: int) -> List[Dict]:
    """Staff replies the passenger has not seen yet."""
    with database._connect() as conn:
        rows = conn.execute(
            "SELECT id, message, created_at FROM conversations WHERE session_id = ? "
            "AND role = 'agent' AND id > ? ORDER BY id ASC", (session_id, after_id)).fetchall()
    return [dict(r) for r in rows]


def status_for_passenger(session_id: str) -> Optional[Dict]:
    case = active(session_id)
    if case is None:
        return None
    return {"reference": reference(case), "status": case["status"]}
