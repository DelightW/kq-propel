"""
Service requests: changes a passenger asks for that staff carry out.

The first kind is a name correction. Before this, the assistant could quote
the fee for correcting a name and take the money, but it never asked which
booking, what the name was or what it should become, and the payment was not
linked to any change. A request now walks through:

    awaiting_booking       the passenger is asked for the booking reference and surname
    awaiting_new_name      the booking is known; the passenger is asked for the new name
    awaiting_confirmation  the change and its fee are summarised for a yes or no
    awaiting_payment       the fee is quoted; the existing payment flow takes over
    submitted              the payment is confirmed; staff see it on the dashboard
    completed              staff have updated the booking in Kenya Airways' system
    cancelled              the passenger said no or changed their mind

The fee is never written into this code. It is read from the Refund Policy
section on name corrections (15/75 US dollars to correct up to three
characters, 50/150 for a full change, domestic/international), so the rule
that the assistant only charges amounts found in policy still holds. The
booking itself is changed by staff, as it would be in a contact centre: the
assistant has no write access to the reservation system.
"""
import re
from datetime import datetime
from typing import Dict, List, Optional

from app import database

AWAITING_BOOKING = "awaiting_booking"
AWAITING_NEW_NAME = "awaiting_new_name"
AWAITING_CONFIRMATION = "awaiting_confirmation"
AWAITING_PAYMENT = "awaiting_payment"
SUBMITTED, COMPLETED, CANCELLED = "submitted", "completed", "cancelled"
IN_DIALOG = (AWAITING_BOOKING, AWAITING_NEW_NAME, AWAITING_CONFIRMATION)
OPEN = IN_DIALOG + (AWAITING_PAYMENT, SUBMITTED)

# "I want to correct my name", "my surname is misspelt on the ticket",
# "please change the name on my booking".
START_RE = re.compile(
    r"\b(?:change|correct|fix|update|amend|edit)\b[^.?!]{0,40}\b(?:name|surname|spelling)\b"
    r"|\b(?:name|surname)\b[^.?!]{0,40}\b(?:misspelt|misspelled|spelt\s+wrong|spelled\s+wrong|"
    r"wrong|incorrect|typo|mistake)\b",
    re.IGNORECASE)
# "How much does it cost to correct a name?" is a policy question, answered as before.
INFO_RE = re.compile(r"\b(how\s+much|cost|costs|fee|fees|price|charge|charges)\b", re.IGNORECASE)
STATUS_RE = re.compile(
    r"\b(?:status|progress|update|happened)\b[^.?!]{0,40}\b(?:name|request|correction|change)\b"
    r"|\b(?:has|is|was)\s+(?:my|the)\s+name\s+(?:been\s+)?(?:changed|corrected|updated|fixed)",
    re.IGNORECASE)
YES_RE = re.compile(r"^\s*(?:yes|yeah|yep|yup|sure|ok(?:ay)?|go\s+ahead|proceed|confirm(?:ed)?|"
                    r"that'?s\s+(?:right|correct)|correct)\b", re.IGNORECASE)
NO_RE = re.compile(r"^\s*(?:no|nope|cancel|stop|never\s*mind|forget\s+it|don'?t)\b", re.IGNORECASE)
_NAME_PREFIX_RE = re.compile(
    r"^\s*(?:(?:please\s+)?(?:change|correct|update)\s+it\s+to|it\s+should\s+(?:be|read)|"
    r"(?:the\s+)?(?:correct|right|new)\s+(?:name|spelling)\s+(?:is|should\s+be)|"
    r"(?:my\s+)?(?:name|surname)\s+(?:is|should\s+be)|make\s+it|to)\s*[:,-]?\s*",
    re.IGNORECASE)
_NAME_WORD_RE = re.compile(r"^[A-Za-z][A-Za-z'-]{0,29}$")
# Everyday words that are not names; a reply made of them is not the new name.
_NOT_NAME_WORDS = frozenset("""
a an the is it its my me i you your we our this that what how much why when where who which can could
would will please thanks thank yes no not cancel stop change correct fix name surname booking fee cost
pay payment help hello hi ok okay wait sorry and or to of for on in at with be been was are am do does
""".split())


def _now() -> str:
    return datetime.utcnow().isoformat()


def reference(row: Dict) -> str:
    return f"SR-{int(row['id']):05d}"


# ------------------------------------------------------------------ storage

def _get(where: str, args: tuple) -> Optional[Dict]:
    with database._connect() as conn:
        row = conn.execute(f"SELECT * FROM service_requests WHERE {where} "
                           f"ORDER BY id DESC LIMIT 1", args).fetchone()
    return dict(row) if row else None


def in_dialog(session_id: str) -> Optional[Dict]:
    marks = ",".join("?" * len(IN_DIALOG))
    return _get(f"session_id = ? AND status IN ({marks})", (session_id, *IN_DIALOG))


def latest(session_id: str) -> Optional[Dict]:
    return _get("session_id = ?", (session_id,))


def get(request_id: int) -> Optional[Dict]:
    return _get("id = ?", (request_id,))


def start(session_id: str, status: str, booking_reference: Optional[str] = None,
          current_name: Optional[str] = None, scope: Optional[str] = None) -> Dict:
    now = _now()
    waiting = in_dialog(session_id)
    if waiting and waiting["status"] == AWAITING_BOOKING:
        # The request that was waiting for the booking carries on, same reference.
        return update(waiting["id"], status=status, booking_reference=booking_reference,
                      current_name=current_name, scope=scope)
    with database._connect() as conn:
        # A new request replaces any unfinished one in this conversation.
        marks = ",".join("?" * len(IN_DIALOG))
        conn.execute(f"UPDATE service_requests SET status = ?, updated_at = ? "
                     f"WHERE session_id = ? AND status IN ({marks})",
                     (CANCELLED, now, session_id, *IN_DIALOG))
        cur = conn.execute(
            "INSERT INTO service_requests (session_id, kind, booking_reference, scope, current_name, "
            "status, created_at, updated_at) VALUES (?, 'name_correction', ?, ?, ?, ?, ?, ?)",
            (session_id, booking_reference, scope, current_name, status, now, now))
        new_id = cur.lastrowid
    return get(new_id)


def update(request_id: int, **fields) -> Dict:
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with database._connect() as conn:
        conn.execute(f"UPDATE service_requests SET {cols} WHERE id = ?", (*fields.values(), request_id))
    return get(request_id)


def on_payment_paid(transaction: Dict) -> None:
    """Called by the payment ledger when an attempt becomes paid."""
    quote_id = transaction.get("quote_id")
    if not quote_id:
        return
    request = _get("quote_id = ? AND status = ?", (quote_id, AWAITING_PAYMENT))
    if request:
        update(request["id"], status=SUBMITTED, payment_reference=transaction.get("reference"))


def list_requests(include_closed: bool = False, limit: int = 50) -> List[Dict]:
    sql = "SELECT * FROM service_requests"
    args: tuple = ()
    if not include_closed:
        sql += " WHERE status IN (?, ?)"
        args = (SUBMITTED, AWAITING_PAYMENT)
    sql += " ORDER BY CASE status WHEN 'submitted' THEN 0 ELSE 1 END, id DESC LIMIT ?"
    with database._connect() as conn:
        rows = conn.execute(sql, (*args, limit)).fetchall()
    return [{**dict(r), "reference": reference(dict(r))} for r in rows]


def complete(request_id: int, username: str) -> Dict:
    request = get(request_id)
    if request is None:
        raise LookupError(request_id)
    if request["status"] != SUBMITTED:
        raise ValueError(f"Only a paid request can be completed (this one is {request['status']}).")
    return update(request_id, status=COMPLETED, completed_by=username)


# ------------------------------------------------------------------ the change itself

def characters_changed(old: str, new: str) -> int:
    """Edit distance between the two names, ignoring case and spacing - the
    measure behind "correcting up to three characters"."""
    a = re.sub(r"\s+", " ", old.strip().upper())
    b = re.sub(r"\s+", " ", new.strip().upper())
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def parse_new_name(message: str, current: str) -> Optional[str]:
    """The name the passenger wants, in title case, or None if the message
    does not look like a name. One word replaces the surname only."""
    if message.strip().endswith("?"):
        return None                     # a question, not a name
    text = _NAME_PREFIX_RE.sub("", message.strip()).strip(" .!\"'")
    words = text.split()
    if not 1 <= len(words) <= 4 or not all(_NAME_WORD_RE.match(w) for w in words):
        return None
    if any(w.lower() in _NOT_NAME_WORDS for w in words):
        return None
    if len(words) == 1:
        given = current.split()[:-1]
        words = given + words
        if len(words) < 2:
            return None
    # Capitalise each part, so hyphenated names such as Wambui-Kanja keep their capitals.
    return " ".join(re.sub(r"[A-Za-z]+", lambda m: m.group(0).capitalize(), w) for w in words)


_FEE_RE = re.compile(
    r"up\s+to\s+(\w+)\s+characters\s+of\s+a\s+name\s+costs\s+(\d+(?:\.\d+)?)\s+US\s+dollars\s+on\s+a\s+"
    r"domestic\s+booking\s+and\s+(\d+(?:\.\d+)?)\s+US\s+dollars\s+on\s+an\s+international\s+booking\.\s+"
    r"A\s+full\s+name\s+change\s+costs\s+(\d+(?:\.\d+)?)\s+US\s+dollars\s+on\s+a\s+domestic\s+booking\s+"
    r"and\s+(\d+(?:\.\d+)?)\s+US\s+dollars\s+on\s+an\s+international\s+booking",
    re.IGNORECASE)
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}


def name_fees(chunks: List[Dict]) -> Optional[Dict]:
    """The name-correction fees as the policy states them, with the section
    they came from, or None when no retrieved section states them."""
    for chunk in chunks:
        text = re.sub(r"\s+", " ", chunk.get("text") or "")
        m = _FEE_RE.search(text)
        if not m:
            continue
        limit = m.group(1).lower()
        limit = _NUMBER_WORDS.get(limit) or (int(limit) if limit.isdigit() else None)
        if not limit:
            continue
        return {"correction_limit": limit, "currency": "USD",
                "correction": {"domestic": float(m.group(2)), "international": float(m.group(3))},
                "change": {"domestic": float(m.group(4)), "international": float(m.group(5))},
                "source": f"{chunk.get('doc_title') or chunk.get('source')} - {chunk.get('section')}"}
    return None


def price(fees: Dict, scope: str, changed: int) -> Dict:
    kind = "correction" if changed <= fees["correction_limit"] else "change"
    return {"kind": kind, "amount": fees[kind][scope], "currency": fees["currency"]}


def description(request: Dict) -> str:
    what = "Name correction" if request.get("change_kind") == "correction" else "Name change"
    return (f"{what} on booking {request.get('booking_reference')}: {request.get('current_name')} to "
            f"{request.get('new_name')} ({reference(request)})")
