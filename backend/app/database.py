"""
Persistence layer for chat transcripts, transactions and sentiment telemetry
backing the Administrative Dashboard.

Two databases are supported behind one interface:

* SQLite (the default): a single file, for a laptop and for the tests.
* PostgreSQL, when DATABASE_URL is set: for the hosted copy, whose own disk
  is wiped on every restart.

Every module writes SQLite-style SQL ("?" placeholders) through _connect().
On PostgreSQL the adapter below translates it, returns rows that behave like
sqlite3.Row (by name and by position), and supplies lastrowid and rowcount,
so no module needs to know which database it is talking to.
"""
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Dict, List, Optional

from app import config

try:  # PostgreSQL support is optional: the laptop and the tests use SQLite.
    import psycopg
    _PG_INTEGRITY = (psycopg.errors.IntegrityError,)
except ImportError:  # pragma: no cover - only without the driver installed
    psycopg = None
    _PG_INTEGRITY = ()

# Catch this, not sqlite3.IntegrityError, so the code works on both databases.
IntegrityError = (sqlite3.IntegrityError,) + _PG_INTEGRITY

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

-- One row per member of staff (Priority 5). The role decides which routes
-- they may call; see app/staff.py.
CREATE TABLE IF NOT EXISTS staff_users (
    username TEXT PRIMARY KEY COLLATE NOCASE,
    display_name TEXT,
    role TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    created_by TEXT,
    last_login_at TEXT
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

-- What the last answered question was about, so a follow-up such as "what
-- about Business class?" or "is that flight delayed?" keeps its context.
CREATE TABLE IF NOT EXISTS conversation_context (
    session_id TEXT PRIMARY KEY,
    last_query TEXT,
    flight_number TEXT,
    booking TEXT,                    -- JSON trip and passenger names (for a name correction); expires with the context
    updated_at TEXT NOT NULL
);

-- Changes a passenger asks for and staff carry out (app/service_requests.py).
-- Holds the old and new name, because that is the change being requested.
CREATE TABLE IF NOT EXISTS service_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL,                 -- name_correction
    booking_reference TEXT,
    scope TEXT,                         -- domestic | international
    current_name TEXT,
    new_name TEXT,
    characters_changed INTEGER,
    change_kind TEXT,                   -- correction | change
    fee_amount REAL,
    fee_currency TEXT,
    fee_source TEXT,
    quote_id TEXT,
    payment_reference TEXT,
    status TEXT NOT NULL,
    completed_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Booking lookups per session, so references cannot be guessed by trial and
-- error (app/booking.py). Holds no reference or name.
CREATE TABLE IF NOT EXISTS booking_lookups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    found INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

-- A passenger asking for a person. Staff replies are stored in
-- `conversations` with role 'agent', so the transcript stays in one place.
CREATE TABLE IF NOT EXISTS handovers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    status TEXT NOT NULL,            -- open | claimed | closed
    reason TEXT,
    frustration_score REAL,
    claimed_by TEXT,
    forwarded TEXT,                  -- result of the contact-centre webhook
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conversations_session
    ON conversations(session_id);
CREATE INDEX IF NOT EXISTS idx_handovers_status
    ON handovers(status, created_at);
CREATE INDEX IF NOT EXISTS idx_transactions_session
    ON transactions(session_id);
CREATE INDEX IF NOT EXISTS idx_admin_sessions_expires
    ON admin_sessions(expires_at);
"""


# ---------------------------------------------------------------------------
# PostgreSQL adapter
# ---------------------------------------------------------------------------

# Tables whose rows are numbered; an INSERT into one returns the new id.
_ID_TABLES = frozenset({"conversations", "transactions", "rag_evaluations", "admin_audit",
                        "service_requests", "booking_lookups", "handovers"})
_INSERT_RE = re.compile(r"^\s*INSERT\s+INTO\s+(\w+)", re.IGNORECASE)
_NOCASE_RE = re.compile(r"([\w.]+)\s*=\s*\?\s+COLLATE\s+NOCASE", re.IGNORECASE)


def using_postgres() -> bool:
    return bool(config.DATABASE_URL)


def _pg_schema() -> str:
    """The SQLite schema in PostgreSQL's dialect. REAL becomes DOUBLE
    PRECISION, because PostgreSQL's REAL is single precision."""
    schema = (_SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
              .replace(" REAL", " DOUBLE PRECISION")
              .replace(" COLLATE NOCASE", ""))
    # Usernames are unique regardless of case, as COLLATE NOCASE makes them in SQLite.
    return schema + ("\nCREATE UNIQUE INDEX IF NOT EXISTS idx_staff_users_username_lower "
                     "ON staff_users (lower(username));\n")


@lru_cache(maxsize=512)
def _pg_sql(sql: str, has_params: bool):
    """Translates one statement. Returns (sql, returns_id)."""
    text = _NOCASE_RE.sub(r"lower(\1) = lower(?)", sql)
    if has_params:
        # psycopg uses %s; a literal % (as in LIKE 'x%') must be doubled.
        text = text.replace("%", "%%").replace("?", "%s")
    match = _INSERT_RE.match(text)
    returns_id = bool(match and match.group(1).lower() in _ID_TABLES
                      and "RETURNING" not in text.upper())
    if returns_id:
        text = text.rstrip().rstrip(";") + " RETURNING id"
    return text, returns_id


class Row(dict):
    """A result row readable by column name or by position, like sqlite3.Row."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _plain(value):
    # AVG() over integers returns NUMERIC; JSON and round() expect a float.
    return float(value) if isinstance(value, Decimal) else value


def _row_factory(cursor):
    names = [d.name for d in cursor.description] if cursor.description else []
    return lambda values: Row(zip(names, (_plain(v) for v in values)))


class _PgCursor:
    def __init__(self, cursor, lastrowid=None):
        self._cursor = cursor
        self.lastrowid = lastrowid
        self.rowcount = cursor.rowcount

    def fetchone(self):
        return self._cursor.fetchone() if self._cursor.description else None

    def fetchall(self):
        return self._cursor.fetchall() if self._cursor.description else []

    def __iter__(self):
        return iter(self.fetchall())


class _PgConnection:
    """The subset of sqlite3.Connection the modules use."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql: str, params=None):
        params = tuple(int(p) if isinstance(p, bool) else p for p in (params or ()))
        text, returns_id = _pg_sql(sql, bool(params))
        cursor = self._conn.cursor(row_factory=_row_factory)
        cursor.execute(text, params or None)
        lastrowid = cursor.fetchone()["id"] if returns_id else None
        return _PgCursor(cursor, lastrowid)

    def executescript(self, script: str):
        self._conn.execute(script)


_pool = None


def _pg_pool():
    global _pool
    if _pool is None:
        if psycopg is None:
            raise RuntimeError("DATABASE_URL is set but psycopg is not installed "
                               "(pip install 'psycopg[binary]' psycopg-pool).")
        from psycopg_pool import ConnectionPool
        # prepare_threshold=None: a pooled endpoint (PgBouncer, as Neon's
        # pooled URL uses) cannot keep prepared statements between requests.
        # check: a hosted database closes idle connections, so each one is
        # tested before it is handed out.
        _pool = ConnectionPool(config.DATABASE_URL, min_size=1, max_size=5, open=True,
                               kwargs={"prepare_threshold": None},
                               check=ConnectionPool.check_connection, max_idle=240)
        import atexit
        atexit.register(close_pool)   # close before shutdown, not in a finaliser
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def _connect():
    if using_postgres():
        with _pg_pool().connection() as conn:
            try:
                yield _PgConnection(conn)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return
    config.APP_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.APP_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _columns(conn, table: str) -> set:
    if using_postgres():
        return {r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ?", (table,))}
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def init_db():
    with _connect() as conn:
        conn.executescript(_pg_schema() if using_postgres() else _SCHEMA)
        _migrate(conn)


def _migrate(conn):
    """Additive migrations for databases created before a column existed.

    CREATE TABLE IF NOT EXISTS silently leaves an existing table untouched, so
    a pre-existing kq_propel.db would otherwise keep the old schema.
    """
    columns = _columns(conn, "rag_evaluations")
    if "metric_version" not in columns:
        conn.execute("ALTER TABLE rag_evaluations ADD COLUMN "
                     "metric_version INTEGER NOT NULL DEFAULT 1")

    tx_columns = _columns(conn, "transactions")
    if "method" not in tx_columns:
        # Every pre-existing row was an M-Pesa STK push in shillings, which is
        # what the defaults record.
        conn.execute("ALTER TABLE transactions ADD COLUMN "
                     "method TEXT NOT NULL DEFAULT 'mpesa'")
    if "currency" not in tx_columns:
        conn.execute("ALTER TABLE transactions ADD COLUMN "
                     "currency TEXT NOT NULL DEFAULT 'KES'")

    state_columns = _columns(conn, "payment_state")
    if "currency" not in state_columns:
        conn.execute("ALTER TABLE payment_state ADD COLUMN "
                     "currency TEXT NOT NULL DEFAULT 'KES'")

    # --- Payment lifecycle (see payments.py) ---
    # amount_minor holds the amount in cents so money is never a float;
    # quote_id ties an attempt to the offer it settles, which is what stops a
    # retry from charging twice; provider_receipt and status_detail record what
    # the provider reported when the payment reached its final status.
    for column, ddl in (
        ("amount_minor", "INTEGER"),
        ("quote_id", "TEXT"),
        ("provider_receipt", "TEXT"),
        ("status_detail", "TEXT"),
        ("updated_at", "TEXT"),
        ("source", "TEXT"),
    ):
        if column not in tx_columns:
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {column} {ddl}")
    conn.execute("UPDATE transactions SET amount_minor = CAST(ROUND(amount * 100) AS INTEGER) "
                 "WHERE amount_minor IS NULL AND amount IS NOT NULL")
    # Statuses written before the lifecycle existed. "initiated" and
    # "awaiting_approval" were requests still waiting on the customer; the
    # simulated_* values were demonstrations and never money.
    conn.execute("UPDATE transactions SET status = 'pending' "
                 "WHERE status IN ('initiated', 'awaiting_approval')")
    conn.execute("UPDATE transactions SET status = 'simulated' WHERE status LIKE 'simulated%'")
    # At most one live attempt per offer. The index only covers pending and
    # paid rows, so a failed or expired attempt can be retried.
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_open_quote "
                 "ON transactions(quote_id) WHERE quote_id IS NOT NULL "
                 "AND status IN ('pending', 'paid')")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_transactions_checkout "
                 "ON transactions(checkout_request_id)")

    state_columns = _columns(conn, "payment_state")
    for column in ("quote_id", "expires_at"):
        if column not in state_columns:
            conn.execute(f"ALTER TABLE payment_state ADD COLUMN {column} TEXT")

    context_columns = _columns(conn, "conversation_context")
    if "booking" not in context_columns:
        conn.execute("ALTER TABLE conversation_context ADD COLUMN booking TEXT")

    # --- Answer feedback and conversation review ---
    # A passenger can rate an answer; the rating lives on the answer's row,
    # so erasing the conversation erases it too. Each answer's scores are
    # linked to its conversation and message, so staff can find the answers
    # that scored worst instead of seeing averages only.
    conv_columns = _columns(conn, "conversations")
    for column in ("feedback", "feedback_note", "feedback_at"):
        if column not in conv_columns:
            conn.execute(f"ALTER TABLE conversations ADD COLUMN {column} TEXT")
    eval_columns = _columns(conn, "rag_evaluations")
    if "session_id" not in eval_columns:
        conn.execute("ALTER TABLE rag_evaluations ADD COLUMN session_id TEXT")
    if "message_id" not in eval_columns:
        conn.execute("ALTER TABLE rag_evaluations ADD COLUMN message_id INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rag_evaluations_session ON rag_evaluations(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rag_evaluations_message ON rag_evaluations(message_id)")

    # --- Chat threads and saved reasoning ---
    # A browser keeps a list of its chats: each session records the digest of
    # the browser's owner cookie, a public thread id (the session id itself is
    # a secret and never leaves the cookie) and whether it is pinned. Each
    # answer keeps the agent's reasoning steps for staff review.
    session_columns = _columns(conn, "chat_sessions")
    for column, ddl in (("owner", "TEXT"), ("thread_id", "TEXT"),
                        ("pinned", "INTEGER NOT NULL DEFAULT 0")):
        if column not in session_columns:
            conn.execute(f"ALTER TABLE chat_sessions ADD COLUMN {column} {ddl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_sessions_owner ON chat_sessions(owner)")
    if "trace" not in _columns(conn, "conversations"):
        conn.execute("ALTER TABLE conversations ADD COLUMN trace TEXT")


# ---------------------------------------------------------------------------
# Staff sessions and audit
# ---------------------------------------------------------------------------

def create_admin_session(token_hash: str, username: str, ttl_minutes: int,
                          ip: str, user_agent: str) -> None:
    now = datetime.utcnow()
    expires = now + timedelta(minutes=ttl_minutes)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO admin_sessions (token_hash, username, created_at, "
            "expires_at, last_seen_at, ip, user_agent) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(token_hash) DO UPDATE SET username=excluded.username, "
            "created_at=excluded.created_at, expires_at=excluded.expires_at, "
            "last_seen_at=excluded.last_seen_at, ip=excluded.ip, user_agent=excluded.user_agent",
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

def create_chat_session(session_id: str, ip: Optional[str] = None,
                        owner: Optional[str] = None) -> None:
    now = datetime.utcnow().isoformat()
    thread_id = "t_" + secrets.token_urlsafe(9)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO chat_sessions (session_id, created_at, last_seen_at, ip, owner, thread_id) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
            "created_at=excluded.created_at, last_seen_at=excluded.last_seen_at, ip=excluded.ip",
            (session_id, now, now, ip, owner, thread_id),
        )


def claim_chat_session(session_id: str, owner: str) -> None:
    """Gives a session made before chat threads existed to this browser's
    owner, and a thread id. A session that already has an owner keeps it."""
    with _connect() as conn:
        conn.execute("UPDATE chat_sessions SET owner = ? WHERE session_id = ? AND owner IS NULL",
                     (owner, session_id))
        conn.execute("UPDATE chat_sessions SET thread_id = ? WHERE session_id = ? AND thread_id IS NULL",
                     ("t_" + secrets.token_urlsafe(9), session_id))


def list_threads(owner: str, limit: int = 50) -> List[Dict]:
    """The owner's chats that have messages, pinned first, then most recent.
    Each carries its first question as the title."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT cs.thread_id, cs.pinned, cs.created_at, "
            "(SELECT message FROM conversations c WHERE c.session_id = cs.session_id AND c.role = 'user' "
            " ORDER BY c.id LIMIT 1) AS title, "
            "(SELECT MAX(created_at) FROM conversations c WHERE c.session_id = cs.session_id) AS last_at "
            "FROM chat_sessions cs WHERE cs.owner = ?", (owner,)).fetchall()
    rows = [dict(r) for r in rows if r["last_at"]]
    rows.sort(key=lambda r: r["last_at"], reverse=True)
    rows.sort(key=lambda r: not r["pinned"])
    return rows[:limit]


def thread_session(owner: str, thread_id: str) -> Optional[str]:
    """The session behind one of the owner's threads, or None."""
    with _connect() as conn:
        row = conn.execute("SELECT session_id FROM chat_sessions WHERE owner = ? AND thread_id = ?",
                           (owner, thread_id)).fetchone()
    return row["session_id"] if row else None


def set_thread_pinned(session_id: str, pinned: bool) -> None:
    with _connect() as conn:
        conn.execute("UPDATE chat_sessions SET pinned = ? WHERE session_id = ?",
                     (1 if pinned else 0, session_id))


def save_trace(message_id: int, trace_json: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE conversations SET trace = ? WHERE id = ?", (trace_json, message_id))


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
                 sources: Optional[str] = None) -> int:
    """Stores one message and returns its id."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO conversations (session_id, role, message, sentiment_label, "
            "frustration_score, sources, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, role, message, sentiment_label, frustration_score, sources,
             datetime.utcnow().isoformat()),
        )
        return cur.lastrowid


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
                        metric_version: int = METRIC_VERSION, session_id: Optional[str] = None,
                        message_id: Optional[int] = None):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO rag_evaluations (query_id, query, context_relevance, groundedness, "
            "answer_relevance, model, metric_version, created_at, session_id, message_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (query_id, query, context_relevance, groundedness, answer_relevance, model,
             metric_version, datetime.utcnow().isoformat(), session_id, message_id),
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
                         currency: str = "KES", quote_id: Optional[str] = None,
                         expires_at: Optional[str] = None):
    """Stores a new payment offer, replacing any earlier one for the session.

    Use payments.new_quote rather than calling this directly: it issues the
    quote identifier and expiry that make the offer safe to settle.
    """
    with _connect() as conn:
        conn.execute(
            "INSERT INTO payment_state (session_id, amount, description, stage, updated_at, "
            "currency, quote_id, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "amount=excluded.amount, description=excluded.description, "
            "stage=excluded.stage, updated_at=excluded.updated_at, currency=excluded.currency, "
            "quote_id=excluded.quote_id, expires_at=excluded.expires_at",
            (session_id, amount, description, stage, datetime.utcnow().isoformat(), currency,
             quote_id, expires_at),
        )


def update_pending_stage(session_id: str, stage: str):
    """Advances an existing offer without changing its amount, quote or expiry."""
    with _connect() as conn:
        conn.execute("UPDATE payment_state SET stage = ?, updated_at = ? WHERE session_id = ?",
                     (stage, datetime.utcnow().isoformat(), session_id))


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
    """Superseded by payments.transition, which enforces the allowed state
    changes. Kept so older tooling keeps importing; it now delegates."""
    from app import payments
    payments.transition_by_checkout(checkout_request_id, status)
