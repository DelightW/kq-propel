"""Name corrections, end to end (booking, new name, fee from policy, payment,
staff completion), and the short-reply sentiment rule.

Payments are stubbed; nothing contacts Safaricom. Temporary database.
Run:  python tools/test_service_requests.py
"""
import sys
import tempfile
import traceback
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402

config.APP_DB_PATH = Path(tempfile.mkdtemp()) / "requests.db"
config.AVIATIONSTACK_API_KEY = ""
config.OPENAI_API_KEY = ""
config.CHAT_MODEL_PROVIDER = "offline"
config.KQ_BOOKING_API_URL = ""
config.ADMIN_PASSWORD_HASH = ""
config.ADMIN_PASSWORD = "test-staff-password"

from app import agent, daraja, database, payments, sentiment, service_requests  # noqa: E402

database.init_db()
daraja.is_configured = lambda: True
daraja.initiate_stk_push = lambda phone_number, amount, reference, description: {
    "source": "daraja_sandbox", "success": True, "amount": 1, "checkout_request_id": f"ws_CO_{reference}",
    "phone_number": phone_number, "reference": reference, "message": "accepted"}
daraja.query_stk_status = lambda checkout_id: {"status": None, "detail": "pending"}
_n = [0]


def session():
    _n[0] += 1
    return f"cs_req_{_n[0]}"


def say(sid, message):
    return agent.run_agent_turn(sid, message)["answer"]


def test_name_parsing_and_counting():
    assert service_requests.characters_changed("Delight Wangui", "Delight Wambui") == 2
    assert service_requests.characters_changed("Delight Wangui", "Delight Kanja") > 3
    assert service_requests.parse_new_name("change it to Delight Wambui", "Delight Wangui") == "Delight Wambui"
    assert service_requests.parse_new_name("Wambui", "Delight Wangui") == "Delight Wambui"
    assert service_requests.parse_new_name("delight wambui-kanja", "X Y") == "Delight Wambui-Kanja"
    for not_a_name in ("How much is it?", "no thanks", "wait", "KQDLW1", "12345"):
        assert service_requests.parse_new_name(not_a_name, "Delight Wangui") is None, not_a_name


def test_fee_comes_from_policy_text():
    chunks = [{"text": open(config.POLICY_DIR / "refund_policy.txt", encoding="utf-8").read(),
               "section": "Section 5", "source": "refund_policy.txt"}]
    fees = service_requests.name_fees(chunks)
    assert fees["correction_limit"] == 3
    assert fees["correction"] == {"domestic": 15.0, "international": 75.0}
    assert fees["change"] == {"domestic": 50.0, "international": 150.0}
    assert service_requests.name_fees([{"text": "nothing about names"}]) is None


def test_full_name_correction_flow():
    s = session()
    assert "booking reference" in say(s, "I want to correct my name")
    found = say(s, "booking reference KQDLW1, surname Wangui")
    assert "Delight Wangui" in found and "What should the name be?" in found
    summary = say(s, "Delight Wambui")
    assert "Delight Wambui" in summary and "USD 75.00" in summary and "2 character(s)" in summary
    req = service_requests.latest(s)
    assert service_requests.reference(req) == "SR-00001", "the waiting request continues, same number"
    assert "SR-00001" in say(s, "yes")
    say(s, "M-Pesa")
    say(s, "0708374149")
    req = service_requests.latest(s)
    assert req["status"] == service_requests.AWAITING_PAYMENT and req["quote_id"]
    with database._connect() as conn:
        tx = dict(conn.execute("SELECT * FROM transactions WHERE quote_id = ?", (req["quote_id"],)).fetchone())
    assert tx["description"] == "Name correction on booking KQDLW1: Delight Wangui to Delight Wambui (SR-00001)"
    payments.transition(tx["id"], payments.PAID, receipt="UKTEST123")
    req = service_requests.latest(s)
    assert req["status"] == service_requests.SUBMITTED and req["payment_reference"] == tx["reference"]
    assert "with our team" in say(s, "What is the status of my name change?")
    service_requests.complete(req["id"], "admin")
    assert "now shows Delight Wambui" in say(s, "Has my name been corrected?")


def test_full_name_change_costs_more():
    s = session()
    say(s, "booking ref KQDLW1 surname Wangui")
    assert "What should the name be?" in say(s, "My name is misspelt on the booking")
    summary = say(s, "Delight Kanja")
    assert "full name change" in summary and "USD 150.00" in summary


def test_cancel_and_policy_question_untouched():
    s = session()
    say(s, "please change the name on my booking")
    assert "cancelled" in say(s, "no")
    assert service_requests.latest(s)["status"] == service_requests.CANCELLED
    answer = say(session(), "How much does it cost to correct my name on a domestic booking?")
    assert "15 US dollars" in answer and "booking reference" not in answer


def test_staff_routes():
    from fastapi.testclient import TestClient
    from app.main import app as fastapi_app
    staff = TestClient(fastapi_app)
    assert staff.get("/api/admin/requests").status_code == 401
    staff.post("/api/admin/login", json={"username": config.ADMIN_USERNAME, "password": config.ADMIN_PASSWORD})
    rows = staff.get("/api/admin/requests?include_closed=true").json()["requests"]
    assert any(r["reference"] == "SR-00001" and r["status"] == "completed" for r in rows), rows
    open_one = next((r for r in rows if r["status"] != "submitted"), rows[0])
    assert staff.post(f"/api/admin/requests/{open_one['id']}/complete").status_code == 409
    actions = [e["action"] for e in staff.get("/api/admin/audit").json()["entries"]]
    assert "view_service_requests" in actions


def test_short_replies_are_not_read_as_frustration():
    for reply in ("yes", "no", "card", "0708374149", "M-Pesa"):
        assert sentiment.classify_frustration(reply)["label"] == "calm", reply
    assert sentiment.classify_frustration("This is ridiculous, where is my bag")["label"] == "frustrated"


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
