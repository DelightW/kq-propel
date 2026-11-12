"""Queries behind the staff review screens.

- Conversations: one row per passenger conversation with its worst scores,
  passenger ratings, frustration and handovers, so staff can find the answers
  that went wrong instead of seeing averages only.
- Trends: the same signals per day.
- Payments and the access log: paged, filtered, and exportable as CSV.

Passenger text is masked (phone numbers) before it leaves this module, and
session identifiers are shortened for display.
"""
import csv
import io
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from app import database, privacy

# An answer is flagged for review when its groundedness is under 0.9: at
# least one claim in it was not found in the policy text. Answer relevance is
# shown but not flagged. Without gold answer keys it measures word overlap
# with the question, and correct answers to the evaluation questions score as
# low as 0.14 (median 0.39), so a threshold on it flags good answers.
LOW_GROUNDEDNESS = 0.9
FRUSTRATED = 0.55
# How a declined answer begins (the out-of-scope reply, or the Swahili one).
DECLINED_PREFIXES = ("I don't have anything on that in the airline's policy documents",
                     "Samahani, bado sielewi Kiswahili")

FLAGS = ("low", "down", "frustrated", "handover", "declined")
MAX_PAGE_SIZE = 100
CSV_MAX_ROWS = 5000


def short_id(session_id: Optional[str]) -> str:
    return (session_id or "")[:8]


def _page(page: int, size: int) -> Tuple[int, int]:
    size = max(1, min(int(size or 25), MAX_PAGE_SIZE))
    page = max(1, int(page or 1))
    return page, size


# ------------------------------------------------------------- conversations

_CONVERSATIONS_SQL = f"""
SELECT * FROM (
  SELECT c.session_id,
         MIN(c.created_at) AS started_at,
         MAX(c.created_at) AS last_at,
         SUM(CASE WHEN c.role = 'user' THEN 1 ELSE 0 END) AS questions,
         MAX(CASE WHEN c.role = 'user' THEN c.frustration_score END) AS max_frustration,
         SUM(CASE WHEN c.feedback = 'up' THEN 1 ELSE 0 END) AS rated_up,
         SUM(CASE WHEN c.feedback = 'down' THEN 1 ELSE 0 END) AS rated_down,
         SUM(CASE WHEN c.role = 'assistant' AND (c.message LIKE ? OR c.message LIKE ?)
                  THEN 1 ELSE 0 END) AS declined,
         (SELECT u.message FROM conversations u WHERE u.session_id = c.session_id
            AND u.role = 'user' ORDER BY u.id LIMIT 1) AS first_question,
         (SELECT MIN(e.groundedness) FROM rag_evaluations e
            WHERE e.session_id = c.session_id AND e.metric_version = ?) AS min_groundedness,
         (SELECT MIN(e.answer_relevance) FROM rag_evaluations e
            WHERE e.session_id = c.session_id AND e.metric_version = ?) AS min_relevance,
         (SELECT COUNT(*) FROM handovers h WHERE h.session_id = c.session_id) AS handovers
  FROM conversations c
  GROUP BY c.session_id
) s
"""


def _flag_clause(flag: Optional[str]) -> str:
    return {
        "low": f"s.min_groundedness < {LOW_GROUNDEDNESS}",
        "down": "s.rated_down > 0",
        "frustrated": f"s.max_frustration >= {FRUSTRATED}",
        "handover": "s.handovers > 0",
        "declined": "s.declined > 0",
    }.get(flag or "", "1 = 1")


def list_conversations(flag: Optional[str] = None, q: Optional[str] = None,
                       page: int = 1, size: int = 25) -> Dict:
    page, size = _page(page, size)
    where = [_flag_clause(flag)]
    args: List = [DECLINED_PREFIXES[0] + "%", DECLINED_PREFIXES[1] + "%",
                  database.METRIC_VERSION, database.METRIC_VERSION]
    if q and q.strip():
        where.append("s.session_id IN (SELECT session_id FROM conversations WHERE message LIKE ?)")
        args.append("%" + q.strip()[:100] + "%")
    sql = _CONVERSATIONS_SQL + " WHERE " + " AND ".join(where)
    with database._connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM ({sql})", args).fetchone()[0]
        rows = [dict(r) for r in conn.execute(
            sql + " ORDER BY s.last_at DESC LIMIT ? OFFSET ?", args + [size, (page - 1) * size])]
    out = []
    for r in rows:
        flags = [f for f in FLAGS if _row_has_flag(r, f)]
        out.append({
            "session_id": r["session_id"], "short_id": short_id(r["session_id"]),
            "started_at": r["started_at"], "last_at": r["last_at"], "questions": r["questions"],
            "first_question": privacy.mask_text(r["first_question"]),
            "min_groundedness": r["min_groundedness"], "min_relevance": r["min_relevance"],
            "max_frustration": r["max_frustration"], "rated_up": r["rated_up"],
            "rated_down": r["rated_down"], "handovers": r["handovers"], "declined": r["declined"],
            "flags": flags,
        })
    return {"conversations": out, "total": total, "page": page, "size": size,
            "thresholds": {"groundedness": LOW_GROUNDEDNESS, "frustration": FRUSTRATED}}


def _row_has_flag(r: Dict, flag: str) -> bool:
    if flag == "low":
        return r["min_groundedness"] is not None and r["min_groundedness"] < LOW_GROUNDEDNESS
    if flag == "down":
        return bool(r["rated_down"])
    if flag == "frustrated":
        return r["max_frustration"] is not None and r["max_frustration"] >= FRUSTRATED
    if flag == "handover":
        return bool(r["handovers"])
    if flag == "declined":
        return bool(r["declined"])
    return False


def conversation_detail(session_id: str) -> Optional[Dict]:
    with database._connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM conversations WHERE session_id = ? ORDER BY id", (session_id,))]
        if not rows:
            return None
        scores = {r["message_id"]: dict(r) for r in conn.execute(
            "SELECT message_id, context_relevance, groundedness, answer_relevance, model "
            "FROM rag_evaluations WHERE session_id = ? AND metric_version = ? AND message_id IS NOT NULL",
            (session_id, database.METRIC_VERSION))}
        cases = [dict(r) for r in conn.execute(
            "SELECT id, status, created_at FROM handovers WHERE session_id = ? ORDER BY id", (session_id,))]
    messages = []
    for r in rows:
        item = {"id": r["id"], "role": r["role"], "message": privacy.mask_text(r["message"]),
                "created_at": r["created_at"]}
        if r["role"] == "user":
            item["sentiment"] = r["sentiment_label"]
            item["frustration_score"] = r["frustration_score"]
        if r["role"] == "assistant":
            item["scores"] = scores.get(r["id"])
            item["feedback"] = r.get("feedback")
            item["feedback_note"] = privacy.mask_text(r.get("feedback_note"))
            item["declined"] = r["message"].startswith(DECLINED_PREFIXES)
        messages.append(item)
    from app import handover
    return {"session_id": session_id, "short_id": short_id(session_id), "messages": messages,
            "handover_cases": [handover.reference(c) for c in cases]}


# ------------------------------------------------------------------- trends

def trends(days: int = 14, today: Optional[datetime] = None) -> Dict:
    days = max(1, min(int(days or 14), 90))
    end = (today or datetime.utcnow()).date()
    start = end - timedelta(days=days - 1)
    since = start.isoformat()
    series = {(start + timedelta(days=i)).isoformat(): {
        "date": (start + timedelta(days=i)).isoformat(), "conversations": 0, "questions": 0,
        "frustrated": 0, "groundedness": None, "answer_relevance": None, "rated_up": 0,
        "rated_down": 0, "handovers": 0, "declined": 0} for i in range(days)}
    with database._connect() as conn:
        for r in conn.execute(
                "SELECT substr(created_at, 1, 10) AS d, COUNT(DISTINCT session_id) AS conv, "
                "SUM(CASE WHEN role = 'user' THEN 1 ELSE 0 END) AS q, "
                "SUM(CASE WHEN role = 'user' AND sentiment_label = 'frustrated' THEN 1 ELSE 0 END) AS f, "
                "SUM(CASE WHEN role = 'assistant' AND (message LIKE ? OR message LIKE ?) THEN 1 ELSE 0 END) AS dec "
                "FROM conversations WHERE created_at >= ? GROUP BY d",
                (DECLINED_PREFIXES[0] + "%", DECLINED_PREFIXES[1] + "%", since)):
            if r["d"] in series:
                series[r["d"]].update(conversations=r["conv"], questions=r["q"] or 0,
                                      frustrated=r["f"] or 0, declined=r["dec"] or 0)
        for r in conn.execute(
                "SELECT substr(created_at, 1, 10) AS d, AVG(groundedness) AS g, AVG(answer_relevance) AS a "
                "FROM rag_evaluations WHERE created_at >= ? AND metric_version = ? GROUP BY d",
                (since, database.METRIC_VERSION)):
            if r["d"] in series:
                series[r["d"]].update(groundedness=round(r["g"], 3), answer_relevance=round(r["a"], 3))
        for r in conn.execute(
                "SELECT substr(feedback_at, 1, 10) AS d, SUM(CASE WHEN feedback = 'up' THEN 1 ELSE 0 END) AS up, "
                "SUM(CASE WHEN feedback = 'down' THEN 1 ELSE 0 END) AS down "
                "FROM conversations WHERE feedback_at >= ? GROUP BY d", (since,)):
            if r["d"] in series:
                series[r["d"]].update(rated_up=r["up"] or 0, rated_down=r["down"] or 0)
        for r in conn.execute(
                "SELECT substr(created_at, 1, 10) AS d, COUNT(*) AS n FROM handovers "
                "WHERE created_at >= ? GROUP BY d", (since,)):
            if r["d"] in series:
                series[r["d"]]["handovers"] = r["n"]
    return {"days": list(series.values())}


# ------------------------------------------------------- payments and audit

def _ledger_where(status: Optional[str], method: Optional[str], q: Optional[str]) -> Tuple[str, List]:
    where, args = ["1 = 1"], []
    if status:
        where.append("status = ?")
        args.append(status)
    if method in ("mpesa", "card"):
        where.append("method = ?")
        args.append(method)
    if q and q.strip():
        where.append("(reference LIKE ? OR session_id LIKE ?)")
        args += [f"%{q.strip()[:60]}%", f"{q.strip()[:60]}%"]
    return " AND ".join(where), args


def ledger(status: Optional[str] = None, method: Optional[str] = None, q: Optional[str] = None,
           page: int = 1, size: int = 25, limit: Optional[int] = None) -> Dict:
    page, size = _page(page, size)
    where, args = _ledger_where(status, method, q)
    with database._connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM transactions WHERE {where}", args).fetchone()[0]
        if limit:
            rows = conn.execute(f"SELECT * FROM transactions WHERE {where} ORDER BY id DESC LIMIT ?",
                                args + [limit]).fetchall()
        else:
            rows = conn.execute(f"SELECT * FROM transactions WHERE {where} ORDER BY id DESC "
                                f"LIMIT ? OFFSET ?", args + [size, (page - 1) * size]).fetchall()
    return {"transactions": privacy.mask_rows([dict(r) for r in rows]), "total": total,
            "page": page, "size": size}


def audit(user: Optional[str] = None, action: Optional[str] = None, page: int = 1, size: int = 50,
          limit: Optional[int] = None) -> Dict:
    page, size = _page(page, size)
    where, args = ["1 = 1"], []
    if user:
        where.append("username = ?")
        args.append(user)
    if action:
        where.append("action = ?")
        args.append(action)
    clause = " AND ".join(where)
    with database._connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM admin_audit WHERE {clause}", args).fetchone()[0]
        sql = (f"SELECT username, action, detail, ip, created_at FROM admin_audit WHERE {clause} "
               f"ORDER BY id DESC LIMIT ? OFFSET ?")
        rows = conn.execute(sql, args + ([limit, 0] if limit else [size, (page - 1) * size])).fetchall()
        users = [r[0] for r in conn.execute("SELECT DISTINCT username FROM admin_audit ORDER BY 1")]
        actions = [r[0] for r in conn.execute("SELECT DISTINCT action FROM admin_audit ORDER BY 1")]
    return {"entries": [dict(r) for r in rows], "total": total, "page": page, "size": size,
            "users": [u for u in users if u], "actions": [a for a in actions if a]}


_CSV_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _cell(value) -> str:
    """A CSV cell that a spreadsheet will not run as a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_CSV_FORMULA_START) else text


def to_csv(rows: List[Dict], columns: List[str]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    for r in rows:
        writer.writerow([_cell(r.get(c)) for c in columns])
    return buf.getvalue()


LEDGER_CSV_COLUMNS = ["created_at", "reference", "session_id", "method", "currency", "amount", "status",
                      "phone_number", "checkout_request_id", "description"]
AUDIT_CSV_COLUMNS = ["created_at", "username", "action", "detail", "ip"]


# ------------------------------------------------------------ knowledge base

def knowledge_base() -> Dict:
    """Every policy document, its provenance and its sections, chunked
    exactly as the index chunks them. Read-only: a policy is changed by
    editing its file, which rebuilds the index on the next start."""
    from app import config, provenance
    from app.vectorstore import chunk_document
    documents = []
    for path in sorted(config.POLICY_DIR.glob("*.txt")):
        chunks = chunk_document(path.read_text(encoding="utf-8"), path.stem)
        meta = provenance.document(path.name) or {}
        sections: List[Dict] = []
        for c in chunks:
            if sections and sections[-1]["section"] == c["section"]:
                sections[-1]["chunks"] += 1
                sections[-1]["text"] += " " + c["text"]
            else:
                sections.append({"section": c["section"], "text": c["text"], "chunks": 1})
        documents.append({
            "file": path.name,
            "title": chunks[0]["doc_title"] if chunks else path.stem,
            "provenance": meta.get("provenance", "undeclared"),
            "source_url": meta.get("source_url"),
            "retrieved_on": meta.get("retrieved_on"),
            "edition": provenance.edition_label(path.name),
            "chunks": len(chunks),
            "sections": sections,
        })
    return {"documents": documents, "total_chunks": sum(d["chunks"] for d in documents)}
