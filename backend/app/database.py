"""
Persistence layer for chat transcripts, transactions and sentiment telemetry
backing the Administrative Dashboard. Uses SQLite for zero-configuration
local persistence (swappable for a managed relational store in production;
document/vector data lives in vectorstore.py per the proposal's MongoDB
design).
"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Dict, List, Optional

from app import config

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
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rag_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query_id TEXT,
    query TEXT,
    context_relevance REAL,
    groundedness REAL,
    answer_relevance REAL,
    model TEXT,
    created_at TEXT NOT NULL
);

-- Tracks a payment the agent has offered but not yet completed, so the
-- passenger can confirm and supply their M-Pesa number across several turns
-- instead of having to state everything in one message.
CREATE TABLE IF NOT EXISTS payment_state (
    session_id TEXT PRIMARY KEY,
    amount REAL,
    description TEXT,
    stage TEXT,
    updated_at TEXT NOT NULL
);
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
                     amount: float, reference: str, description: str, status: str):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO transactions (session_id, checkout_request_id, phone_number, "
            "amount, reference, description, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, checkout_request_id, phone_number, amount, reference, description,
             status, datetime.utcnow().isoformat()),
        )


def log_rag_evaluation(query_id: str, query: str, context_relevance: float,
                        groundedness: float, answer_relevance: float, model: str):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO rag_evaluations (query_id, query, context_relevance, groundedness, "
            "answer_relevance, model, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (query_id, query, context_relevance, groundedness, answer_relevance, model,
             datetime.utcnow().isoformat()),
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


def fetch_rag_evaluations(limit: int = 100) -> List[Dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM rag_evaluations ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def set_pending_payment(session_id: str, amount: float, description: str, stage: str):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO payment_state (session_id, amount, description, stage, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
            "amount=excluded.amount, description=excluded.description, "
            "stage=excluded.stage, updated_at=excluded.updated_at",
            (session_id, amount, description, stage, datetime.utcnow().isoformat()),
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
