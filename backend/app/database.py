"""
Persistence layer for chat transcripts, transactions and sentiment telemetry
backing the Administrative Dashboard. Uses SQLite for zero-configuration
local persistence (swappable for a managed relational store in production;
document/vector data lives in vectorstore.py per the proposal's MongoDB
design).
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from app import config

# Incremented whenever the evaluation metrics change in a way that makes new
# scores incomparable with stored ones. Version 2 replaced bag-of-words
# overlap with claim-level groundedness, bounded context relevance and
# gold-key answer relevance.
METRIC_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    message TEXT NOT NULL,
    sentiment_label TEXT,
    frustration_score REAL,
    sources TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    checkout_request_id TEXT,
    phone_number TEXT,
    amount REAL,
    reference TEXT,
    description TEXT,
    status TEXT,
    created_at TEXT NOT NULL,
    -- Which rail settled the money, and in what currency. Without these a
    -- shilling STK push and a dollar card charge are indistinguishable rows.
    method TEXT NOT NULL DEFAULT 'mpesa',
    currency TEXT NOT NULL DEFAULT 'KES'
);

-- `metric_version` records which evaluation implementation produced a row.
-- Version 1 used bag-of-words overlap, which scored fabrications as grounded
-- and averaged an unbounded ranking score; those rows are not comparable with
-- version 2 and are excluded from reported aggregates rather than deleted, so
-- the correction remains auditable.
CREATE TABLE IF NOT EXISTS rag_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query_id TEXT,
    query TEXT,
    context_relevance REAL,
    groundedness REAL,
    answer_relevance REAL,
    model TEXT,
    metric_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

-- Tracks a payment the agent has offered but not yet completed, so the
-- passenger can confirm and choose a rail across several turns instead of
-- having to state everything in one message.
--
-- `amount`/`currency` hold the fee exactly as the policy publishes it. The
-- shilling figure is derived only when the guest chooses M-Pesa, so a guest
-- paying by card is never quoted a converted amount.
CREATE TABLE IF NOT EXISTS payment_state (
    session_id TEXT PRIMARY KEY,
    amount REAL,
    description TEXT,
    stage TEXT,
    updated_at TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'KES'
);

-- Staff sessions issued after a successful login. Only the SHA-256 digest of
-- the token is stored, so read access to this file does not yield a usable
-- credential. Rows are server-side state rather than a stateless signed
-- token specifically so that logout and forced revocation are possible.
CREATE TABLE IF NOT EXISTS admin_sessions (
    token_hash TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    ip TEXT,
    user_agent TEXT
);

-- Who looked at passenger data, and when. The transaction ledger contains
-- phone numbers, so administrative reads are themselves events worth
-- recording.
CREATE TABLE IF NOT EXISTS admin_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT,
    action TEXT NOT NULL,
    detail TEXT,
    ip TEXT,
    created_at TEXT NOT NULL
);

-- Passenger sessions. Previously the client invented its own identifier and
-- the server trusted it, which meant conversation history and pending payment
-- state could be addressed by anyone who supplied the right string.
CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    ip TEXT
);

CREATE INDEX IF NOT EXISTS idx_conversations_session
    ON conversations(session_id);
CREATE INDEX IF NOT EXISTS idx_transactions_session
    ON transactions(session_id);
CREATE INDEX IF NOT EXISTS idx_admin_sessions_expires
    ON admin_sessions(expires_at);
"""


@contextmanager
def _connect():
    config.APP_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.APP_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        _migrate(conn)


def _migrate(conn):
    """Additive migrations for databases created before a column existed.

    CREATE TABLE IF NOT EXISTS silently leaves an existing table untouched, so
    a pre-existing kq_propel.db would otherwise keep the old schema.
    """
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(rag_evaluations)")}
    if "metric_version" not in columns:
        conn.execute("ALTER TABLE rag_evaluations ADD COLUMN "
                     "metric_version INTEGER NOT NULL DEFAULT 1")

    tx_columns = {r["name"] for r in conn.execute("PRAGMA table_info(transactions)")}
    if "method" not in tx_columns:
        # Every pre-existing row was an M-Pesa STK push in shillings, which is
        # what the defaults record.
        conn.execute("ALTER TABLE transactions ADD COLUMN "
                     "method TEXT NOT NULL DEFAULT 'mpesa'")
    if "currency" not in tx_columns:
        conn.execute("ALTER TABLE transactions ADD COLUMN "
                     "currency TEXT NOT NULL DEFAULT 'KES'")

    state_columns = {r["name"] for r in conn.execute("PRAGMA table_info(payment_state)")}
    if "currency" not in state_columns:
        conn.execute("ALTER TABLE payment_state ADD COLUMN "
                     "currency TEXT NOT NULL DEFAULT 'KES'")


# ---------------------------------------------------------------------------
# Staff sessions and audit
# ---------------------------------------------------------------------------

def create_admin_session(token_hash: str, username: str, ttl_minutes: int,
                          ip: str, user_agent: str) -> None:
    now = datetime.utcnow()
    expires = now + timedelta(minutes=ttl_minutes)
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO admin_sessions (token_hash, username, created_at, "
            "expires_at, last_seen_at, ip, user_agent) VALUES (?,?,?,?,?,?,?)",
            (token_hash, username, now.isoformat(), expires.isoformat(),
             now.isoformat(), ip, user_agent),
        )


def get_admin_session(token_hash: str) -> Optional[Dict]:
    """Returns the session only while it is still valid.

    Expiry is enforced on read rather than by a background job, so a stale row
    can never authorise a request even if cleanup has not run.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM admin_sessions WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        if not row:
            return None
        try:
            expires = datetime.fromisoformat(row["expires_at"])
        except ValueError:
            expires = datetime.utcnow() - timedelta(seconds=1)
        if expires <= datetime.utcnow():
            conn.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (token_hash,))
            return None
        return dict(row)


def touch_admin_session(token_hash: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE admin_sessions SET last_seen_at = ? WHERE token_hash = ?",
                     (datetime.utcnow().isoformat(), token_hash))


def delete_admin_session(token_hash: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (token_hash,))


def purge_expired_admin_sessions() -> int:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM admin_sessions WHERE expires_at <= ?",
                            (datetime.utcnow().isoformat(),))
        return cur.rowcount or 0


def record_admin_audit(username: Optional[str], action: str,
                        detail: Optional[str] = None, ip: Optional[str] = None) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO admin_audit (username, action, detail, ip, created_at) "
            "VALUES (?,?,?,?,?)",
            (username, action, detail, ip, datetime.utcnow().isoformat()),
        )


def fetch_admin_audit(limit: int = 50) -> List[Dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT username, action, detail, ip, created_at FROM admin_audit "
            "ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Passenger sessions
# ---------------------------------------------------------------------------

def create_chat_session(session_id: str, ip: Optional[str] = None) -> None:
    now = datetime.utcnow().isoformat()
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO chat_sessions (session_id, created_at, "
            "last_seen_at, ip) VALUES (?,?,?,?)", (session_id, now, now, ip),
        )


def get_chat_session(session_id: str) -> Optional[Dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM chat_sessions WHERE session_id = ?",
                            (session_id,)).fetchone()
        if not row:
            return None
        conn.execute("UPDATE chat_sessions SET last_seen_at = ? WHERE session_id = ?",
                      (datetime.utcnow().isoformat(), session_id))
        return dict(row)


def log_message(session_id: str, role: str, message: str,
                 sentiment_label: Optional[str] = None,
                 frustration_score: Optional[float] = None,
                 sources: Optional[str] = None):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO conversations (session_id, role, message, sentiment_label, "
            "frustration_score, sources, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, role, message, sentiment_label, frustration_score, sources,
             datetime.utcnow().isoformat()),
        )


def log_transaction(session_id: str, checkout_request_id: str, phone_number: str,
                     amount: float, reference: str, description: str, status: str,
                     method: str = "mpesa", currency: str = "KES"):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO transactions (session_id, checkout_request_id, phone_number, "
            "amount, reference, description, status, created_at, method, currency) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, checkout_request_id, phone_number, amount, reference, description,
             status, datetime.utcnow().isoformat(), method, currency),
        )


def log_rag_evaluation(query_id: str, query: str, context_relevance: float,
                        groundedness: float, answer_relevance: float, model: str,
                        metric_version: int = METRIC_VERSION):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO rag_evaluations (query_id, query, context_relevance, groundedness, "
            "answer_relevance, model, metric_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (query_id, query, context_relevance, groundedness, answer_relevance, model,
             metric_version, datetime.utcnow().isoformat()),
        )


def fetch_conversation(session_id: str) -> List[Dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM conversations WHERE session_id = ? ORDER BY id ASC", (session_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def fetch_transactions(limit: int = 50) -> List[Dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM transactions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def fetch_sentiment_distribution() -> Dict:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT sentiment_label, COUNT(*) as cnt FROM conversations "
            "WHERE sentiment_label IS NOT NULL GROUP BY sentiment_label"
        ).fetchall()
        return {r["sentiment_label"]: r["cnt"] for r in rows}


def fetch_rag_evaluations(limit: int = 100,
                          metric_version: Optional[int] = METRIC_VERSION) -> List[Dict]:
    """Most recent evaluations. Defaults to the current metric version so that
    aggregates are never computed across incompatible implementations; pass
    metric_version=None to retrieve the full history including superseded rows."""
    with _connect() as conn:
        if metric_version is None:
            rows = conn.execute(
                "SELECT * FROM rag_evaluations ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM rag_evaluations WHERE metric_version = ? "
                "ORDER BY id DESC LIMIT ?", (metric_version, limit)
            ).fetchall()
        return [dict(r) for r in rows]


def set_pending_payment(session_id: str, amount: float, description: str, stage: str,
                         currency: str = "KES"):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO payment_state (session_id, amount, description, stage, updated_at, "
            "currency) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
            "amount=excluded.amount, description=excluded.description, "
            "stage=excluded.stage, updated_at=excluded.updated_at, currency=excluded.currency",
            (session_id, amount, description, stage, datetime.utcnow().isoformat(), currency),
        )


def get_pending_payment(session_id: str) -> Optional[Dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM payment_state WHERE session_id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row else None


def clear_pending_payment(session_id: str):
    with _connect() as conn:
        conn.execute("DELETE FROM payment_state WHERE session_id = ?", (session_id,))


def update_transaction_status(checkout_request_id: str, status: str):
    with _connect() as conn:
        conn.execute(
            "UPDATE transactions SET status = ? WHERE checkout_request_id = ?",
            (status, checkout_request_id),
        )
