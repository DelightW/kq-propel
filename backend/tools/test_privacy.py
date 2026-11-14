"""Data protection: masking, erasure on request and retention by age.

Runs against a temporary database; never touches backend/data/kq_propel.db.
Run:  python tools/test_privacy.py
"""
import sys
import tempfile
import traceback
from datetime import datetime, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402

config.APP_DB_PATH = Path(tempfile.mkdtemp()) / "privacy.db"
config.AVIATIONSTACK_API_KEY = ""
config.OPENAI_API_KEY = ""
config.CHAT_MODEL_PROVIDER = "offline"
config.HANDOVER_ENABLED = True
config.HANDOVER_WEBHOOK_URL = ""
config.ADMIN_PASSWORD_HASH = ""
config.ADMIN_PASSWORD = "test-staff-password"

from app import database, privacy  # noqa: E402

database.init_db()
NOW = datetime(2026, 11, 10, 12, 0, 0)


def ago(**kw) -> str:
    return (NOW - timedelta(**kw)).isoformat()


def seed(session, when, *, phone="254700000001", status="paid", handover_status=None,
         question=None):
    """One passenger session with everything personal the system can hold."""
    question = question or f"What is my allowance? ({session})"
    with database._connect() as conn:
        conn.execute("INSERT INTO chat_sessions VALUES (?,?,?,?)", (session, when, when, "10.0.0.1"))
        for role, text in (("user", question), ("user", f"My number is 0700 000 001 ({session})"),
                           ("assistant", "Two pieces of 23kg.")):
            conn.execute("INSERT INTO conversations (session_id, role, message, created_at) "
                         "VALUES (?,?,?,?)", (session, role, text, when))
        conn.execute("INSERT INTO conversation_context (session_id, last_query, flight_number, updated_at) "
                     "VALUES (?,?,?,?)", (session, question, "KQ100", when))
        conn.execute("INSERT INTO payment_state (session_id, amount, description, stage, updated_at) "
                     "VALUES (?,?,?,?,?)", (session, 75, "fee", "offered", when))
        conn.execute("INSERT INTO transactions (session_id, checkout_request_id, phone_number, amount, "
                     "reference, description, status, created_at, method, currency) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (session, "ws_" + session, phone, 9675, "KQ-" + session, "fee", status, when,
                      "mpesa", "KES"))
        conn.execute("INSERT INTO rag_evaluations (query_id, query, context_relevance, groundedness, "
                     "answer_relevance, model, metric_version, created_at) VALUES (?,?,?,?,?,?,?,?)",
                     ("e_" + session, question, 0.4, 1.0, 0.6, "test", database.METRIC_VERSION, when))
        if handover_status:
            conn.execute("INSERT INTO handovers (session_id, status, reason, created_at, updated_at) "
                         "VALUES (?,?,?,?,?)", (session, handover_status, "asked", when, when))


def count(table, session):
    with database._connect() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE session_id = ?", (session,)).fetchone()[0]


def tx(session):
    with database._connect() as conn:
        return dict(conn.execute("SELECT * FROM transactions WHERE session_id = ?", (session,)).fetchone())


def eval_query(session):
    with database._connect() as conn:
        return conn.execute("SELECT query, groundedness FROM rag_evaluations WHERE query_id = ?",
                            ("e_" + session,)).fetchone()


# ------------------------------------------------------------------ masking

def test_mask_phone_keeps_only_prefix_and_last_digits():
    # An obviously made-up number; the repository is public.
    assert privacy.mask_phone("254700000001") == "2547*****001"
    assert privacy.mask_phone("+254 700 000 001") == "2547*****001"
    assert privacy.mask_phone(None) is None and privacy.mask_phone("") == ""


def test_checkout_id_does_not_reveal_the_number():
    # Real Safaricom IDs end with the payer's number (here Safaricom's
    # published sandbox number, 254708374149).
    masked = privacy.mask_checkout_id("ws_CO_11112026124017474708374149")
    assert masked == "ws_CO_11112026124017************", masked
    assert "708374149" not in privacy.mask_checkout_id("ws_CO_101120261807405708374149")
    assert "374149" not in privacy.mask_checkout_id("ws_CO_SIM_254708374149")
    assert privacy.mask_checkout_id("5O190127TN364715T") == "5O190127TN364715T"   # PayPal: untouched
    assert privacy.mask_checkout_id(None) is None and privacy.mask_checkout_id("") == ""


def test_mask_text_finds_numbers_in_messages():
    for raw, number in (("Send it to 0700 000 001 please", "0700000001"),
                        ("my line is +254700000001.", "254700000001"),
                        ("use 0110-000-002", "0110000002")):
        masked = privacy.mask_text(raw)
        digits = "".join(ch for ch in masked if ch.isdigit())
        assert "*" in masked and number not in digits, masked
    assert privacy.mask_text("Flight KQ100 at gate 14, ref 7061234567890") == \
        "Flight KQ100 at gate 14, ref 7061234567890"   # not phone numbers: untouched


# ------------------------------------------------------------------ erasure

def test_erasure_removes_personal_data_and_keeps_the_financial_record():
    seed("cs_erase", ago(days=1))
    seed("cs_bystander", ago(days=1))
    counts = privacy.erase_sessions(["cs_erase"])
    assert counts["messages"] == 3 and counts["payment_contacts"] == 1, counts
    for table in ("conversations", "conversation_context", "payment_state", "chat_sessions", "handovers"):
        assert count(table, "cs_erase") == 0, table
    record = tx("cs_erase")
    assert record["phone_number"] is None and record["amount"] == 9675 and record["status"] == "paid"
    q = eval_query("cs_erase")
    assert q["query"] is None and q["groundedness"] == 1.0   # scores kept, text gone
    assert count("conversations", "cs_bystander") == 3 and tx("cs_bystander")["phone_number"]


def test_sessions_found_by_any_spelling_of_the_number():
    seed("cs_phone", ago(days=2), phone="254733000111")
    assert "cs_phone" in privacy.sessions_for_phone("0733 000 111")
    assert "cs_phone" in privacy.sessions_for_phone("+254733000111")
    # Mentioned in a message only (0700 000 001 is in every seeded chat).
    assert "cs_phone" in privacy.sessions_for_phone("0700000001")
    assert privacy.sessions_for_phone("12") == []


# ------------------------------------------------------------------ retention

def test_retention_purges_by_age_and_spares_what_must_stay():
    config.CONVERSATION_RETENTION_DAYS, config.PAYMENT_CONTACT_RETENTION_DAYS = 30, 90
    seed("cs_old", ago(days=40))
    seed("cs_recent", ago(days=5))
    seed("cs_waiting", ago(days=40), handover_status="open")   # staff have not answered yet
    seed("cs_old_pay", ago(days=100), status="paid")
    seed("cs_old_pending", ago(days=100), status="pending")
    with database._connect() as conn:
        conn.execute("INSERT INTO admin_audit (username, action, created_at) VALUES ('a','old',?)",
                     (ago(days=400),))
        conn.execute("INSERT INTO admin_audit (username, action, created_at) VALUES ('a','new',?)",
                     (ago(days=10),))

    counts = privacy.purge_expired(now=NOW)

    assert count("conversations", "cs_old") == 0 and count("chat_sessions", "cs_old") == 0
    assert count("conversations", "cs_recent") == 3, "recent conversation kept"
    assert count("conversations", "cs_waiting") == 3, "open handover keeps its conversation"
    assert tx("cs_old")["phone_number"] is None, "erased session loses its number"
    assert tx("cs_recent")["phone_number"], "recent payment keeps its number"
    assert tx("cs_old_pay")["phone_number"] is None and tx("cs_old_pay")["amount"] == 9675
    assert eval_query("cs_old")["query"] is None and eval_query("cs_recent")["query"]
    with database._connect() as conn:
        actions = {r[0] for r in conn.execute("SELECT action FROM admin_audit")}
    assert actions == {"new"}, actions
    assert counts["audit_entries"] == 1 and privacy.last_purge()["at"]


def test_pending_payment_keeps_its_number_until_it_settles():
    # cs_old_pending is 100 days old but still pending: its session went with
    # the conversation purge, yet the number is needed to settle the payment.
    # Erasure of the session clears it; age-based purging of the payment
    # contact alone never touches a pending attempt.
    with database._connect() as conn:
        conn.execute("UPDATE transactions SET phone_number = '254700000001' WHERE session_id = 'cs_old_pending'")
    privacy.purge_expired(now=NOW)
    assert tx("cs_old_pending")["phone_number"] == "254700000001"


# ------------------------------------------------------------------ HTTP

def test_passenger_can_erase_their_own_conversation():
    from fastapi.testclient import TestClient
    from app.main import app as fastapi_app

    passenger = TestClient(fastapi_app)
    old_sid = passenger.post("/api/chat/session").json()["session_id"]
    passenger.post("/api/chat", json={"message": "How many bags can I check in Economy to London?"})
    assert count("conversations", old_sid) == 2
    assert passenger.post("/api/chat/forget", json={"confirm": False}).status_code == 400
    r = passenger.post("/api/chat/forget", json={"confirm": True})
    assert r.status_code == 200, r.text
    assert count("conversations", old_sid) == 0 and count("chat_sessions", old_sid) == 0
    assert r.json()["session_id"] != old_sid
    # The new cookie works for the next question.
    assert passenger.post("/api/chat", json={"message": "When does check-in close?"}).status_code == 200
    stranger = TestClient(fastapi_app)
    assert stranger.post("/api/chat/forget", json={"confirm": True}).status_code == 401


def test_staff_see_masked_numbers_and_erasure_is_audited():
    from fastapi.testclient import TestClient
    from app.main import app as fastapi_app

    seed("cs_staff", ago(days=1), phone="254799888777", handover_status="open")
    staff = TestClient(fastapi_app)
    assert staff.post("/api/admin/privacy/erase", json={"phone": "0799888777"}).status_code == 401
    assert staff.post("/api/admin/login", json={"username": config.ADMIN_USERNAME,
                                                "password": config.ADMIN_PASSWORD}).status_code == 200
    # A real-shaped checkout ID ends with the payer's number; staff must not
    # be able to read the number from it on the dashboard or a receipt.
    with database._connect() as conn:
        conn.execute("UPDATE transactions SET checkout_request_id = ? WHERE session_id = ?",
                     ("ws_CO_11112026124017474799888777", "cs_staff"))
    rows = staff.get("/api/admin/transactions").json()
    assert all("888" not in (r["phone_number"] or "")[4:-3] for r in rows), rows
    assert any(r["phone_number"] == "2547*****777" for r in rows), rows
    assert all("799888777" not in (r["checkout_request_id"] or "") for r in rows), rows
    overview = staff.get("/api/admin/overview").json()
    assert "799888777" not in str(overview), "the overview's recent payments are masked too"
    import io
    from pypdf import PdfReader
    receipt = staff.get(f"/api/admin/transactions/{tx('cs_staff')['reference']}/receipt")
    assert receipt.status_code == 200, receipt.text
    text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(receipt.content)).pages)
    assert "799888777" not in text and "ws_CO_11112026124017" in text, text
    cases = staff.get("/api/admin/handovers").json()["cases"]
    case = next(c for c in cases if c["session_id"] == "cs_staff")
    detail = staff.get(f"/api/admin/handovers/{case['id']}").json()
    assert all("345 678" not in m["message"] for m in detail["transcript"]), detail["transcript"]

    preview = staff.post("/api/admin/privacy/erase", json={"phone": "0799 888 777"}).json()
    assert preview == {"confirmed": False, "sessions_found": 1}, preview
    assert count("conversations", "cs_staff") == 3, "a preview erases nothing"
    done = staff.post("/api/admin/privacy/erase",
                      json={"case_reference": case["reference"], "confirm": True}).json()
    assert done["confirmed"] and count("conversations", "cs_staff") == 0, done
    entries = staff.get("/api/admin/audit").json()["entries"]
    erase = next(e for e in entries if e["action"] == "erase_passenger_data")
    assert "799888777" not in (erase["detail"] or ""), erase
    assert any(e["action"] == "preview_erasure" and "2547*****777" in e["detail"] for e in entries)
    assert staff.post("/api/admin/privacy/erase", json={"case_reference": "nonsense"}).status_code == 422


def test_privacy_notice_reflects_configuration():
    from fastapi.testclient import TestClient
    from app.main import app as fastapi_app

    client = TestClient(fastapi_app)
    settings = client.get("/api/chat/privacy").json()
    assert settings["conversation_days"] == config.CONVERSATION_RETENTION_DAYS
    assert settings["payment_contact_days"] == config.PAYMENT_CONTACT_RETENTION_DAYS
    page = client.get("/privacy")
    assert page.status_code == 200 and "Privacy Notice" in page.text


def main() -> int:
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception:
            failures += 1
            print(f"  FAIL  {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
