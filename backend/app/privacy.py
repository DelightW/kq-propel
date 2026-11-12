"""
Data protection: retention, erasure and masking.

Kenya's Data Protection Act, 2019 requires personal data to be kept no longer
than necessary, erased on a valid request, and shown only to people who need
it. This module is where those rules are enforced in code. The retention
periods are configuration, not policy: the defaults below are placeholders
until Kenya Airways' data protection impact assessment sets the real ones
(see docs/DPIA_draft.md).

What counts as personal data here:

  conversations           passenger messages (may contain names, phone numbers,
                          booking details) and staff replies
  chat_sessions.ip        the passenger's IP address
  conversation_context    the last question and flight number
  payment_state           a fee offer in progress
  handovers               a request for a person, with its frustration score
  transactions.phone_number
                          the M-Pesa number. The rest of a payment record
                          (amount, currency, reference, status) is a financial
                          record the airline must keep, so it is retained and
                          only the phone number is removed.
  rag_evaluations.query   the passenger's question, stored with its scores.
                          The scores are kept; the text is removed.
  admin_audit             who looked at passenger data. Kept longer than the
                          data itself, because it is the accountability record.

Erasure deletes or blanks all of the above for a session, and is always
available. Retention does the same by age, at start-up and then every
PRIVACY_PURGE_INTERVAL_MINUTES, once PRIVACY_RETENTION_ENABLED is switched on.
"""
import re
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional

from app import config, database

# Kenyan mobile numbers as passengers type them: 07XX XXX XXX, 01XX..., 2547...,
# +254 7..., with optional spaces or hyphens. Longer digit runs (card numbers,
# references) are left alone by the digit guards.
PHONE_RE = re.compile(r"(?<![\d+])(?:\+?254|0)[\s-]?[17]\d{2}[\s-]?\d{3}[\s-]?\d{3}(?!\d)")

_last_purge: Dict = {}


def _now() -> datetime:
    return datetime.utcnow()


# ------------------------------------------------------------------ masking

def mask_phone(value: Optional[str]) -> Optional[str]:
    """2547XXXXX678 -> 2547*****678. Staff can still tell two numbers apart
    and confirm the last digits with a caller, without seeing the number."""
    if not value:
        return value
    digits = re.sub(r"\D", "", str(value))
    if len(digits) == 10 and digits.startswith("0"):
        digits = "254" + digits[1:]   # 0712... and 254712... mask the same way
    if len(digits) < 7:
        return "*" * len(digits)
    return digits[:4] + "*" * (len(digits) - 7) + digits[-3:]


def mask_text(text: Optional[str]) -> Optional[str]:
    """Masks every phone number inside free text, such as a chat message."""
    if not text:
        return text
    return PHONE_RE.sub(lambda m: mask_phone(m.group(0)), text)


# Safaricom's checkout request ID is "ws_CO_", a 14-digit timestamp, and then
# digits that end with the payer's phone number (ws_CO_111120261240174747
# 08374149 for 254708374149), so showing it in full undoes the phone mask.
_CHECKOUT_RE = re.compile(r"^(ws_CO_\d{14})(\d+)$")
_TRAILING_DIGITS_RE = re.compile(r"^(.*?\D)?(\d{9,})$")


def mask_checkout_id(value: Optional[str]) -> Optional[str]:
    """ws_CO_11112026124017474708374149 -> ws_CO_11112026124017************.
    The timestamp is kept so staff can still match a payment to Safaricom's
    records by time and reference; the digits that hold the number are not."""
    if not value:
        return value
    value = str(value)
    m = _CHECKOUT_RE.match(value)
    if m:
        return m.group(1) + "*" * len(m.group(2))
    # Any other identifier that ends in a phone-length run of digits.
    m = _TRAILING_DIGITS_RE.match(value)
    if m:
        digits = m.group(2)
        return (m.group(1) or "") + digits[:-9] + "*" * 9
    return value


def mask_rows(rows: Iterable[Dict], phone_fields=("phone_number",),
              text_fields=("message", "last_passenger_message"),
              id_fields=("checkout_request_id",)) -> List[Dict]:
    out = []
    for row in rows:
        row = dict(row)
        for f in phone_fields:
            if f in row:
                row[f] = mask_phone(row[f])
        for f in id_fields:
            if f in row:
                row[f] = mask_checkout_id(row[f])
        for f in text_fields:
            if f in row:
                row[f] = mask_text(row[f])
        out.append(row)
    return out


# ------------------------------------------------------------------ erasure

def _session_user_messages(conn, session_ids: List[str]) -> List[str]:
    marks = ",".join("?" * len(session_ids))
    return [r["message"] for r in conn.execute(
        f"SELECT message FROM conversations WHERE role = 'user' AND session_id IN ({marks})",
        session_ids).fetchall()]


def erase_sessions(session_ids: Iterable[str]) -> Dict[str, int]:
    """Erases everything personal that belongs to the given sessions.

    Payment records stay (amount, currency, reference, status) because they
    are financial records, but lose their phone number.
    """
    ids = sorted({s for s in session_ids if s})
    counts = {"sessions": len(ids), "messages": 0, "handover_cases": 0, "payment_contacts": 0,
              "evaluation_texts": 0}
    if not ids:
        return counts
    marks = ",".join("?" * len(ids))
    with database._connect() as conn:
        questions = _session_user_messages(conn, ids)
        if questions:
            qmarks = ",".join("?" * len(questions))
            counts["evaluation_texts"] = conn.execute(
                f"UPDATE rag_evaluations SET query = NULL WHERE query IN ({qmarks})",
                questions).rowcount
        counts["messages"] = conn.execute(
            f"DELETE FROM conversations WHERE session_id IN ({marks})", ids).rowcount
        counts["handover_cases"] = conn.execute(
            f"DELETE FROM handovers WHERE session_id IN ({marks})", ids).rowcount
        counts["payment_contacts"] = conn.execute(
            f"UPDATE transactions SET phone_number = NULL WHERE session_id IN ({marks}) "
            f"AND phone_number IS NOT NULL", ids).rowcount
        for table in ("conversation_context", "payment_state", "booking_lookups", "service_requests",
                      "chat_sessions"):
            conn.execute(f"DELETE FROM {table} WHERE session_id IN ({marks})", ids)
    return counts


def sessions_for_phone(phone: str) -> List[str]:
    """Sessions that used this number to pay or mentioned it in a message.
    Used when a passenger asks the contact centre to erase their data and
    no longer has the chat open."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) < 9:
        return []
    tail = digits[-9:]          # 7XXXXXXXX, the part every spelling shares
    found = set()
    with database._connect() as conn:
        for r in conn.execute("SELECT session_id, phone_number FROM transactions "
                              "WHERE phone_number IS NOT NULL").fetchall():
            if re.sub(r"\D", "", r["phone_number"]).endswith(tail):
                found.add(r["session_id"])
        for r in conn.execute("SELECT session_id, message FROM conversations "
                              "WHERE role = 'user'").fetchall():
            for m in PHONE_RE.finditer(r["message"] or ""):
                if re.sub(r"\D", "", m.group(0)).endswith(tail):
                    found.add(r["session_id"])
    return sorted(found)


# ------------------------------------------------------------------ retention

def retention_policy() -> Dict:
    return {
        "enabled": config.PRIVACY_RETENTION_ENABLED,
        "conversation_days": config.CONVERSATION_RETENTION_DAYS,
        "payment_contact_days": config.PAYMENT_CONTACT_RETENTION_DAYS,
        "audit_days": config.AUDIT_RETENTION_DAYS,
        "purge_interval_minutes": config.PRIVACY_PURGE_INTERVAL_MINUTES,
    }


def purge_expired(now: Optional[datetime] = None) -> Dict[str, int]:
    """Deletes or blanks personal data older than its retention period.

    Sessions are removed as a whole once their last activity is past the
    conversation period, so a long conversation is never cut in half. An open
    or claimed handover case keeps its session until staff close it.
    """
    now = now or _now()
    conv_cut = (now - timedelta(days=config.CONVERSATION_RETENTION_DAYS)).isoformat()
    pay_cut = (now - timedelta(days=config.PAYMENT_CONTACT_RETENTION_DAYS)).isoformat()
    audit_cut = (now - timedelta(days=config.AUDIT_RETENTION_DAYS)).isoformat()
    memory_cut = (now - timedelta(minutes=config.CONVERSATION_MEMORY_MINUTES)).isoformat()

    with database._connect() as conn:
        stale = [r["session_id"] for r in conn.execute(
            "SELECT s.session_id FROM chat_sessions s WHERE s.last_seen_at < ? "
            "AND NOT EXISTS (SELECT 1 FROM handovers h WHERE h.session_id = s.session_id "
            "AND h.status IN ('open', 'claimed'))", (conv_cut,)).fetchall()]
        # Messages whose session row has already gone (sessions created before
        # this module existed, or removed by hand) age out on their own date.
        orphans = [r["session_id"] for r in conn.execute(
            "SELECT DISTINCT c.session_id FROM conversations c WHERE c.created_at < ? AND NOT "
            "EXISTS (SELECT 1 FROM chat_sessions s WHERE s.session_id = c.session_id) AND NOT "
            "EXISTS (SELECT 1 FROM handovers h WHERE h.session_id = c.session_id "
            "AND h.status IN ('open', 'claimed'))", (conv_cut,)).fetchall()]

    counts = erase_sessions(stale + orphans)
    counts["sessions"] = len(stale)
    with database._connect() as conn:
        counts["evaluation_texts"] += conn.execute(
            "UPDATE rag_evaluations SET query = NULL WHERE query IS NOT NULL AND created_at < ?",
            (conv_cut,)).rowcount
        # The payment record is kept; only the number goes, and only once the
        # attempt has a final outcome a dispute could still be raised about.
        counts["payment_contacts"] += conn.execute(
            "UPDATE transactions SET phone_number = NULL WHERE phone_number IS NOT NULL "
            "AND created_at < ? AND status NOT IN ('pending')", (pay_cut,)).rowcount
        counts["expired_memory"] = conn.execute(
            "DELETE FROM conversation_context WHERE updated_at < ?", (memory_cut,)).rowcount
        counts["audit_entries"] = conn.execute(
            "DELETE FROM admin_audit WHERE created_at < ?", (audit_cut,)).rowcount

    _last_purge.clear()
    _last_purge.update({"at": now.isoformat(timespec="seconds"), **counts})
    return counts


def last_purge() -> Dict:
    return dict(_last_purge)
