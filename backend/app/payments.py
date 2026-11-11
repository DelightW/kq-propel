"""
Payment lifecycle: quotes, attempts, provider outcomes and reconciliation.

A payment passes through these states:

    pending ──> paid
            ├─> failed
            ├─> cancelled      (the customer declined on the handset or checkout)
            ├─> expired        (no outcome within PAYMENT_PENDING_TTL_MINUTES)
            └─> needs_review   (the provider reported success for a different amount)

    simulated                  (demonstration mode; never money, never changes)

Rules this module enforces:

  * Only `pending` can change. A final state is never overwritten, so a late or
    repeated callback cannot turn a failure into a payment or the reverse.
  * Money is stored in minor units (cents), never as a float.
  * Every attempt belongs to a quote: the fee the agent read from policy, held
    for PAYMENT_QUOTE_TTL_MINUTES. A quote has at most one open attempt (a
    partial unique index in database.py), so a second "yes", a double-click or
    a retried request returns the existing attempt instead of charging twice.
  * Only `paid` counts as settled money. Pending, failed and simulated attempts
    are reported separately.
"""
import sqlite3
import uuid
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, List, Optional, Tuple

from app import config, database, daraja, paypal

PENDING, PAID, FAILED, CANCELLED, EXPIRED = "pending", "paid", "failed", "cancelled", "expired"
NEEDS_REVIEW, SIMULATED = "needs_review", "simulated"
FINAL = frozenset({PAID, FAILED, CANCELLED, EXPIRED, NEEDS_REVIEW})
ALL_STATUSES = FINAL | {PENDING, SIMULATED}


def to_minor(amount) -> int:
    """Amount in cents, rounded half up. Decimal avoids float artefacts such as
    9675.0 * 100 becoming 967499.9999."""
    return int((Decimal(str(amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_minor(minor: Optional[int]) -> float:
    return float(Decimal(minor or 0) / 100)


def _now() -> datetime:
    return datetime.utcnow()


# ------------------------------------------------------------------ quotes

def new_quote(session_id: str, amount: float, currency: str, description: str,
              stage: str = "offered") -> str:
    """Records a fee offer the passenger may accept, and returns its quote id."""
    quote_id = "Q" + uuid.uuid4().hex[:12].upper()
    expires = _now() + timedelta(minutes=config.PAYMENT_QUOTE_TTL_MINUTES)
    database.set_pending_payment(session_id, amount, description, stage, currency=currency,
                                 quote_id=quote_id, expires_at=expires.isoformat())
    return quote_id


def quote_expired(pending: Dict) -> bool:
    expires = pending.get("expires_at")
    if not expires:
        # Offers stored before quotes existed have no expiry; treat them as
        # expired so nobody settles an amount quoted under the old rules.
        return True
    return _now() >= datetime.fromisoformat(expires)


# ------------------------------------------------------------------ attempts

def _row(conn, where: str, args: tuple) -> Optional[Dict]:
    row = conn.execute(f"SELECT * FROM transactions WHERE {where} ORDER BY id DESC LIMIT 1",
                       args).fetchone()
    return dict(row) if row else None


def open_attempt(quote_id: Optional[str]) -> Optional[Dict]:
    """The pending or paid attempt for a quote, if one exists."""
    if not quote_id:
        return None
    with database._connect() as conn:
        return _row(conn, "quote_id = ? AND status IN (?, ?)", (quote_id, PENDING, PAID))


def record_attempt(*, session_id: str, quote_id: Optional[str], method: str, currency: str,
                   amount: float, result: Dict, phone_number: str = "",
                   reference: str = "", description: str = "") -> Tuple[Dict, bool]:
    """Writes a provider attempt to the ledger.

    Returns (row, created). When the quote already has an open attempt, that
    attempt is returned with created=False and nothing new is written.
    """
    source = result.get("source") or ""
    if source == SIMULATED:
        status = SIMULATED
    elif result.get("success"):
        status = PENDING
    else:
        status = FAILED
    checkout = result.get("checkout_request_id") or result.get("order_id") or ""
    now = _now().isoformat()
    try:
        with database._connect() as conn:
            cur = conn.execute(
                "INSERT INTO transactions (session_id, checkout_request_id, phone_number, amount, "
                "reference, description, status, created_at, method, currency, amount_minor, "
                "quote_id, status_detail, updated_at, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, checkout, phone_number, from_minor(to_minor(amount)), reference,
                 description, status, now, method, currency, to_minor(amount),
                 quote_id if status in (PENDING, PAID) else None,
                 result.get("error"), now, source))
            return _row(conn, "id = ?", (cur.lastrowid,)), True
    except sqlite3.IntegrityError:
        existing = open_attempt(quote_id)
        if existing is None:
            raise
        return existing, False


def transition(row_id: int, new_status: str, *, detail: Optional[str] = None,
               receipt: Optional[str] = None) -> Tuple[bool, Dict]:
    """Moves a pending attempt to `new_status`. Returns (changed, row).

    Anything not pending is left exactly as it is: final states are final and
    simulated attempts never become money.
    """
    if new_status not in ALL_STATUSES:
        raise ValueError(f"unknown payment status: {new_status}")
    with database._connect() as conn:
        cur = conn.execute(
            "UPDATE transactions SET status = ?, status_detail = COALESCE(?, status_detail), "
            "provider_receipt = COALESCE(?, provider_receipt), updated_at = ? "
            "WHERE id = ? AND status = ?",
            (new_status, detail, receipt, _now().isoformat(), row_id, PENDING))
        changed = cur.rowcount == 1
        row = _row(conn, "id = ?", (row_id,))
    if changed and new_status == PAID:
        # A paid fee for a service request (a name correction) submits it to staff.
        from app import service_requests
        service_requests.on_payment_paid(row)
    return changed, row


def find_by_checkout(method: str, checkout_id: str) -> Optional[Dict]:
    with database._connect() as conn:
        return _row(conn, "method = ? AND checkout_request_id = ?", (method, checkout_id))


def transition_by_checkout(checkout_id: str, new_status: str) -> bool:
    with database._connect() as conn:
        row = _row(conn, "checkout_request_id = ?", (checkout_id,))
    return bool(row) and transition(row["id"], new_status)[0]


def for_passenger(session_id: str, reference: str) -> Optional[Dict]:
    """A payment the given session owns, or None. Sessions never see each
    other's payments."""
    with database._connect() as conn:
        return _row(conn, "session_id = ? AND reference = ?", (session_id, reference))


# ------------------------------------------------------------------ provider outcomes

def apply_mpesa_outcome(outcome: Dict) -> Tuple[bool, Optional[Dict]]:
    """Applies a parsed Daraja callback (see daraja.parse_callback).

    A success is accepted only when the amount Safaricom reports matches the
    amount charged and, when verification is enabled, Safaricom's status query
    agrees. A matching but unconfirmed success leaves the payment pending for
    reconciliation rather than marking it paid on the callback's word alone.
    """
    row = find_by_checkout("mpesa", outcome["checkout_request_id"])
    if (row is not None and row["status"] == PAID and not row.get("provider_receipt")
            and outcome["status"] == PAID and outcome.get("receipt")
            and outcome.get("amount") is not None
            and to_minor(outcome["amount"]) == row["amount_minor"]):
        # The status query often confirms a payment before the callback
        # arrives, and only the callback carries the M-Pesa receipt number
        # finance needs. The receipt is added; the status is never changed.
        with database._connect() as conn:
            conn.execute("UPDATE transactions SET provider_receipt = ? WHERE id = ?",
                         (str(outcome["receipt"])[:40], row["id"]))
        return False, find_by_checkout("mpesa", outcome["checkout_request_id"])
    if row is None or row["status"] != PENDING:
        return False, row

    status, detail = outcome["status"], outcome.get("detail")
    if status == PAID:
        reported = outcome.get("amount")
        if reported is None or to_minor(reported) != row["amount_minor"]:
            return transition(row["id"], NEEDS_REVIEW,
                              detail=f"Safaricom reported {reported}, expected "
                                     f"{from_minor(row['amount_minor'])}",
                              receipt=outcome.get("receipt"))
        if config.DARAJA_VERIFY_CALLBACKS:
            confirmed = daraja.query_stk_status(outcome["checkout_request_id"])
            if confirmed.get("status") != PAID:
                return False, row
    return transition(row["id"], status, detail=detail, receipt=outcome.get("receipt"))


def apply_card_outcome(order_id: str, outcome: Dict) -> Tuple[bool, Optional[Dict]]:
    """Applies a PayPal capture result (see paypal.capture_order)."""
    row = find_by_checkout("card", order_id)
    if row is None or row["status"] != PENDING or not outcome.get("status"):
        return False, row
    if outcome["status"] == PAID:
        same_amount = outcome.get("amount") is not None and \
            to_minor(outcome["amount"]) == row["amount_minor"]
        same_currency = (outcome.get("currency") or "").upper() == (row["currency"] or "").upper()
        if not (same_amount and same_currency):
            return transition(row["id"], NEEDS_REVIEW,
                              detail=f"PayPal reported {outcome.get('currency')} "
                                     f"{outcome.get('amount')}",
                              receipt=outcome.get("receipt"))
    return transition(row["id"], outcome["status"], detail=outcome.get("detail"),
                      receipt=outcome.get("receipt"))


def reconcile(row: Dict) -> Dict:
    """Brings one pending attempt up to date with its provider, then expires it
    if it has been pending longer than PAYMENT_PENDING_TTL_MINUTES."""
    if row.get("status") != PENDING:
        return row
    checkout = row.get("checkout_request_id")
    if checkout and row.get("method") == "mpesa" and daraja.is_configured():
        result = daraja.query_stk_status(checkout)
        if result.get("status"):
            row = transition(row["id"], result["status"], detail=result.get("detail"))[1]
    elif checkout and row.get("method") == "card" and paypal.is_configured():
        apply_card_outcome(checkout, paypal.get_order_outcome(checkout))
        row = find_by_checkout("card", checkout) or row
    if row.get("status") == PENDING:
        age = _now() - datetime.fromisoformat(row["created_at"])
        if age > timedelta(minutes=config.PAYMENT_PENDING_TTL_MINUTES):
            row = transition(row["id"], EXPIRED,
                             detail="No outcome from the provider before the time limit.")[1]
    return row


def reconcile_pending(limit: int = 50) -> int:
    """Reconciles the oldest pending attempts. Returns how many changed."""
    with database._connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM transactions WHERE status = ? ORDER BY id ASC LIMIT ?",
            (PENDING, limit)).fetchall()]
    return sum(1 for r in rows if reconcile(r).get("status") != PENDING)


# ------------------------------------------------------------------ reporting

def summary(limit_recent: int = 10) -> Dict:
    """Ledger totals for the dashboard. Settled money is paid attempts only,
    per currency, so failures, pending requests and demonstrations can never
    inflate the figure."""
    with database._connect() as conn:
        by_status = conn.execute(
            "SELECT status, currency, COUNT(*) AS n, COALESCE(SUM(amount_minor), 0) AS minor "
            "FROM transactions GROUP BY status, currency").fetchall()
        by_method = conn.execute(
            "SELECT method, COUNT(*) AS n FROM transactions GROUP BY method").fetchall()
        recent = [dict(r) for r in conn.execute(
            "SELECT * FROM transactions ORDER BY id DESC LIMIT ?", (limit_recent,)).fetchall()]

    settled: Dict[str, float] = {}
    pending: Dict[str, float] = {}
    counts: Dict[str, int] = {}
    for r in by_status:
        counts[r["status"]] = counts.get(r["status"], 0) + r["n"]
        code = (r["currency"] or "KES").upper()
        if r["status"] == PAID:
            settled[code] = settled.get(code, 0.0) + from_minor(r["minor"])
        elif r["status"] == PENDING:
            pending[code] = pending.get(code, 0.0) + from_minor(r["minor"])
    return {
        "count": sum(counts.values()),
        "counts_by_status": counts,
        "counts_by_method": {r["method"] or "mpesa": r["n"] for r in by_method},
        "settled_by_currency": settled,
        "pending_by_currency": pending,
        "recent": recent,
    }
