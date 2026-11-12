"""Priority 5: staff accounts with roles, and rate limits.

Runs the real FastAPI app in-process (TestClient) against a temporary
database. Payments are stubbed; nothing contacts Safaricom.

Run:  python tools/test_priority5.py
"""
import sys
import tempfile
import traceback
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402

config.APP_DB_PATH = Path(tempfile.mkdtemp()) / "p5.db"
config.OPENAI_API_KEY = ""
config.CHAT_MODEL_PROVIDER = "offline"
config.AVIATIONSTACK_API_KEY = ""
config.KQ_BOOKING_API_URL = ""
config.ADMIN_USERNAME = "admin"
config.ADMIN_PASSWORD_HASH = ""
config.ADMIN_PASSWORD = "first-admin-password"

from fastapi.testclient import TestClient  # noqa: E402

from app import agent, auth, daraja, database, ratelimit, staff  # noqa: E402
from app.main import app  # noqa: E402

database.init_db()
daraja.is_configured = lambda: True
daraja.initiate_stk_push = lambda phone_number, amount, reference, description: {
    "source": "daraja_sandbox", "success": True, "amount": 1, "checkout_request_id": f"ws_CO_{reference}",
    "phone_number": phone_number, "reference": reference, "message": "accepted"}
daraja.query_stk_status = lambda checkout_id: {"status": None, "detail": "pending"}


def signed_in(username, password):
    c = TestClient(app)
    r = c.post("/api/admin/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c, r.json()


ADMIN_ONLY = ["/api/admin/audit", "/api/admin/privacy", "/api/admin/staff"]
AGENT_ROUTES = ["/api/admin/transactions", "/api/admin/handovers", "/api/admin/requests"]


# ------------------------------------------------------------------ staff accounts

def test_first_admin_comes_from_the_environment():
    admin, who = signed_in("admin", "first-admin-password")
    assert who["role"] == "admin" and "staff" in who["panels"] and not who["must_change_password"]
    for path in ADMIN_ONLY + AGENT_ROUTES + ["/api/admin/overview"]:
        assert admin.get(path).status_code == 200, path
    assert TestClient(app).post("/api/admin/login", json={"username": "admin", "password": "wrong"}
                                ).status_code == 401


def test_new_account_must_change_its_one_time_password():
    admin, _ = signed_in("admin", "first-admin-password")
    created = admin.post("/api/admin/staff", json={"username": "wambui", "display_name": "Wambui K.",
                                                   "role": "agent"}).json()
    otp = created["one_time_password"]
    assert created["must_change_password"] and "password_hash" not in created
    listed = admin.get("/api/admin/staff").json()["staff"]
    assert all("password_hash" not in s for s in listed) and all(otp not in str(s) for s in listed)

    agent_c, who = signed_in("wambui", otp)
    assert who["must_change_password"]
    assert agent_c.get("/api/admin/requests").status_code == 403, "nothing else until it is changed"
    short = agent_c.post("/api/admin/password", json={"current_password": otp, "new_password": "short"})
    assert short.status_code == 400
    ok = agent_c.post("/api/admin/password", json={"current_password": otp,
                                                   "new_password": "wambui-own-pass"})
    assert ok.status_code == 200 and not ok.json()["must_change_password"]
    assert agent_c.get("/api/admin/requests").status_code == 200


def test_roles_limit_what_each_person_can_reach():
    admin, _ = signed_in("admin", "first-admin-password")
    otp = admin.post("/api/admin/staff", json={"username": "kanja", "role": "analyst"}).json()["one_time_password"]
    analyst, _ = signed_in("kanja", otp)
    analyst.post("/api/admin/password", json={"current_password": otp, "new_password": "kanja-own-pass"})
    agent_c, who = signed_in("wambui", "wambui-own-pass")
    assert who["panels"] == staff.PANELS["agent"]

    for path in ADMIN_ONLY:
        assert agent_c.get(path).status_code == 403, path
        assert analyst.get(path).status_code == 403, path
    for path in AGENT_ROUTES:
        assert agent_c.get(path).status_code == 200, path
        assert analyst.get(path).status_code == 403, "analysts see no passenger records: " + path
    overview = analyst.get("/api/admin/overview")
    assert overview.status_code == 200 and overview.json()["transactions"]["recent"] == []
    assert agent_c.post("/api/admin/staff", json={"username": "x", "role": "admin"}).status_code == 403
    assert agent_c.post("/api/admin/privacy/erase", json={"phone": "0708374149"}).status_code == 403


def test_disabling_signs_out_at_once_and_is_audited():
    admin, _ = signed_in("admin", "first-admin-password")
    agent_c, _ = signed_in("wambui", "wambui-own-pass")
    assert admin.post("/api/admin/staff/wambui/disable").status_code == 200
    assert agent_c.get("/api/admin/requests").status_code == 401, "existing session ends"
    assert TestClient(app).post("/api/admin/login", json={"username": "wambui",
                                                          "password": "wambui-own-pass"}).status_code == 401
    admin.post("/api/admin/staff/wambui/enable")
    signed_in("wambui", "wambui-own-pass")
    actions = [(e["username"], e["action"]) for e in admin.get("/api/admin/audit").json()["entries"]]
    for expected in [("admin", "create_staff"), ("admin", "disable_staff"), ("admin", "enable_staff"),
                     ("wambui", "change_own_password"), ("wambui", "login_success")]:
        assert expected in actions, expected


def test_role_change_applies_immediately():
    admin, _ = signed_in("admin", "first-admin-password")
    agent_c, _ = signed_in("wambui", "wambui-own-pass")
    assert agent_c.get("/api/admin/audit").status_code == 403
    admin.post("/api/admin/staff/wambui/role", json={"role": "admin"})
    assert agent_c.get("/api/admin/audit").status_code == 200, "same session, new role"
    admin.post("/api/admin/staff/wambui/role", json={"role": "agent"})
    assert agent_c.get("/api/admin/audit").status_code == 403


def test_there_is_always_an_administrator():
    admin, _ = signed_in("admin", "first-admin-password")
    assert admin.post("/api/admin/staff/admin/disable").status_code == 409, "not yourself"
    assert admin.post("/api/admin/staff/admin/role", json={"role": "agent"}).status_code == 409, "last admin"
    assert admin.post("/api/admin/staff", json={"username": "admin", "role": "agent"}).status_code == 409
    assert admin.post("/api/admin/staff", json={"username": "bad name!", "role": "agent"}).status_code == 409
    assert admin.post("/api/admin/staff", json={"username": "ok", "role": "pilot"}).status_code == 409


def test_password_reset_revokes_sessions():
    admin, _ = signed_in("admin", "first-admin-password")
    agent_c, _ = signed_in("wambui", "wambui-own-pass")
    otp = admin.post("/api/admin/staff/wambui/reset-password").json()["one_time_password"]
    assert agent_c.get("/api/admin/requests").status_code == 401
    _, who = signed_in("wambui", otp)
    assert who["must_change_password"]


def test_environment_account_password_is_managed_in_the_environment():
    admin, _ = signed_in("admin", "first-admin-password")
    r = admin.post("/api/admin/password", json={"current_password": "first-admin-password",
                                                "new_password": "something-else-long"})
    assert r.status_code == 400 and "environment" in r.json()["detail"]


# ------------------------------------------------------------------ rate limits

# ------------------------------------------- feedback, review, paging, export

def account(username, role):
    """A fresh signed-in staff member with their own password."""
    admin, _ = signed_in("admin", "first-admin-password")
    otp = admin.post("/api/admin/staff", json={"username": username, "role": role}).json()["one_time_password"]
    client, _ = signed_in(username, otp)
    client.post("/api/admin/password", json={"current_password": otp, "new_password": username + "-own-pass"})
    return client


def passenger():
    c = TestClient(app)
    sid = c.post("/api/chat/session").json()["session_id"]
    return c, sid


def test_feedback_and_history_belong_to_their_conversation():
    c, sid = passenger()
    answer = c.post("/api/chat", json={"message": "How many bags can I check in Economy to London?"}).json()
    mid = answer["message_id"]
    assert mid
    rated = c.post("/api/chat/feedback", json={"message_id": mid, "rating": "down",
                                               "note": "Wrong, call me on 0708374149"})
    assert rated.status_code == 200, rated.text
    history = c.get("/api/chat/history").json()["messages"]
    assert [m["role"] for m in history] == ["user", "assistant"] and history[1]["feedback"] == "down"
    # Another passenger can neither rate it nor read it.
    other, _ = passenger()
    assert other.post("/api/chat/feedback", json={"message_id": mid, "rating": "up"}).status_code == 404
    assert other.get("/api/chat/history").json()["messages"] == []
    # Only answers can be rated, only up or down, and the note is bounded.
    assert c.post("/api/chat/feedback", json={"message_id": history[0]["id"], "rating": "up"}).status_code == 404
    assert c.post("/api/chat/feedback", json={"message_id": mid, "rating": "meh"}).status_code == 422
    assert c.post("/api/chat/feedback", json={"message_id": mid, "rating": "up",
                                              "note": "x" * 301}).status_code == 422
    assert TestClient(app).get("/api/chat/history").status_code == 401


def test_staff_review_conversations_by_role():
    c, sid = passenger()
    mid = c.post("/api/chat", json={"message": "How many bags can I check in Economy to London?"}).json()["message_id"]
    c.post("/api/chat/feedback", json={"message_id": mid, "rating": "down", "note": "Ring 0708374149"})
    admin, _ = signed_in("admin", "first-admin-password")
    listed = admin.get("/api/admin/conversations?flag=down").json()
    row = next(r for r in listed["conversations"] if r["session_id"] == sid)
    assert "down" in row["flags"] and row["questions"] == 1 and row["min_groundedness"] == 1.0
    assert "low" not in row["flags"], "a fully grounded answer is not flagged on answer relevance alone"
    assert all("down" in r["flags"] for r in listed["conversations"])
    assert admin.get("/api/admin/conversations?flag=nonsense").status_code == 422
    detail = admin.get(f"/api/admin/conversations/{sid}").json()
    reply = [m for m in detail["messages"] if m["role"] == "assistant"][0]
    assert reply["feedback"] == "down" and reply["scores"]["groundedness"] == 1.0
    assert "0708374149" not in reply["feedback_note"], "phone numbers are masked for staff"
    searched = admin.get("/api/admin/conversations?q=Economy%20to%20London").json()
    assert any(r["session_id"] == sid for r in searched["conversations"])

    agent_c, analyst = account("wanjiru.agent", "agent"), account("kanja.analyst", "analyst")
    assert agent_c.get(f"/api/admin/conversations/{sid}").status_code == 200
    assert analyst.get("/api/admin/conversations").status_code == 403, "no passenger records for analysts"
    assert agent_c.post(f"/api/admin/conversations/{sid}/erase").status_code == 403
    for client in (admin, agent_c, analyst):
        assert client.get("/api/admin/trends").status_code == 200
        assert client.get("/api/admin/knowledge").status_code == 200

    assert admin.post(f"/api/admin/conversations/{sid}/erase").status_code == 200
    assert admin.get(f"/api/admin/conversations/{sid}").status_code == 404
    with database._connect() as conn:
        left = conn.execute("SELECT query FROM rag_evaluations WHERE session_id = ?", (sid,)).fetchall()
    assert left and all(r["query"] is None for r in left), "scores stay, question text goes"
    actions = [e["action"] for e in admin.get("/api/admin/audit?size=100").json()["entries"]]
    assert "view_conversation" in actions and "erase_conversation" in actions


def test_trends_and_knowledge_base():
    admin, _ = signed_in("admin", "first-admin-password")
    days = admin.get("/api/admin/trends?days=14").json()["days"]
    assert len(days) == 14 and days[-1]["questions"] >= 1 and days[-1]["groundedness"] is not None
    assert admin.get("/api/admin/trends?days=5000").json()["days"].__len__() == 90, "bounded"
    kb = admin.get("/api/admin/knowledge").json()
    assert len(kb["documents"]) == 10 and kb["total_chunks"] == 53
    synthetic = [d for d in kb["documents"] if d["provenance"] == "synthetic"]
    assert len(synthetic) == 2


def test_payments_and_access_log_page_filter_and_export():
    for i in range(30):
        database.log_transaction(f"pg-session-{i:02d}", f"ws_CO_1211202610150125470837{i:04d}", "254708374149",
                                 1, f"KQT-{i:04d}", "=HYPERLINK(\"http://x\")" if i == 0 else "demo",
                                 "failed" if i % 3 == 0 else "paid", method="card" if i % 5 == 0 else "mpesa")
    admin, _ = signed_in("admin", "first-admin-password")
    page1 = admin.get("/api/admin/ledger?size=10").json()
    assert page1["total"] >= 30 and len(page1["transactions"]) == 10
    page2 = admin.get("/api/admin/ledger?size=10&page=2").json()["transactions"]
    assert {t["reference"] for t in page2}.isdisjoint({t["reference"] for t in page1["transactions"]})
    failed = admin.get("/api/admin/ledger?status_filter=failed&size=100").json()["transactions"]
    assert failed and all(t["status"] == "failed" for t in failed)
    one = admin.get("/api/admin/ledger?q=KQT-0007").json()
    assert one["total"] == 1 and one["transactions"][0]["phone_number"] != "254708374149", "masked"
    csv_text = admin.get("/api/admin/ledger.csv?method=card").text
    lines = csv_text.strip().splitlines()
    assert lines[0].startswith("created_at,reference") and len(lines) - 1 >= 6
    assert "254708374149" not in csv_text, "numbers stay masked in the export"
    assert "'=HYPERLINK" in csv_text, "a spreadsheet must not run a cell as a formula"

    agent_c, analyst = account("wangui.agent", "agent"), account("delight.analyst", "analyst")
    assert agent_c.get("/api/admin/ledger").status_code == 200
    assert analyst.get("/api/admin/ledger").status_code == 403
    assert analyst.get("/api/admin/ledger.csv").status_code == 403

    audit = admin.get("/api/admin/audit?size=5&page=2").json()
    assert len(audit["entries"]) == 5 and audit["total"] > 10 and "admin" in audit["users"]
    only = admin.get("/api/admin/audit?action=export_payments").json()["entries"]
    assert only and all(e["action"] == "export_payments" for e in only)
    assert admin.get("/api/admin/audit.csv").text.startswith("created_at,username,action")
    assert agent_c.get("/api/admin/audit.csv").status_code == 403


def test_chat_messages_are_limited_per_conversation():
    ratelimit.reset()
    c = TestClient(app)
    c.post("/api/chat/session")
    codes = [c.post("/api/chat", json={"message": "hello"}).status_code
             for _ in range(config.RATE_LIMIT_CHAT_PER_MINUTE + 1)]
    assert codes[:-1] == [200] * config.RATE_LIMIT_CHAT_PER_MINUTE and codes[-1] == 429, codes
    limited = c.post("/api/chat", json={"message": "hello"})
    assert int(limited.headers["Retry-After"]) > 0 and "Please wait" in limited.json()["detail"]
    other = TestClient(app)
    other.post("/api/chat/session")
    assert other.post("/api/chat", json={"message": "hello"}).status_code == 200, "others unaffected"


def test_new_sessions_are_limited_per_address():
    ratelimit.reset()
    codes = [TestClient(app).post("/api/chat/session").status_code
             for _ in range(config.RATE_LIMIT_SESSIONS_PER_10_MIN_PER_IP + 1)]
    assert codes[-1] == 429 and codes.count(200) == config.RATE_LIMIT_SESSIONS_PER_10_MIN_PER_IP
    ratelimit.reset()


def test_mpesa_prompts_are_limited_per_number():
    ratelimit.reset()
    limit = config.RATE_LIMIT_MPESA_PROMPTS_PER_10_MIN
    trace = []
    sent = [agent._execute_payment(f"cs_p5_{i}", "0708374149", 75, "fee", trace)
            for i in range(limit + 1)]
    assert all(r["success"] for r in sent[:limit])
    blocked = sent[-1]
    assert blocked.get("rate_limited") and "Nothing has been charged" in blocked["message"]
    assert "card" in blocked["message"], "offers the other way to pay"
    # The same number written another way is the same number.
    assert agent._execute_payment("cs_p5_x", "+254 708 374 149", 75, "fee", [])["rate_limited"]
    assert agent._execute_payment("cs_p5_y", "0712345678", 75, "fee", [])["success"], "other numbers fine"
    ratelimit.reset()


def test_forwarded_address_uses_only_trusted_hops():
    class Req:
        def __init__(self, xff, peer="10.0.0.1"):
            self.headers = {"x-forwarded-for": xff}
            self.client = type("C", (), {"host": peer})()
    saved = config.TRUSTED_PROXY_HOPS
    try:
        config.TRUSTED_PROXY_HOPS = 0
        assert ratelimit.client_ip(Req("1.2.3.4")) == "10.0.0.1", "header ignored when not behind a proxy"
        config.TRUSTED_PROXY_HOPS = 1
        assert ratelimit.client_ip(Req("6.6.6.6, 41.90.1.2")) == "41.90.1.2", "a forged left entry is ignored"
        assert ratelimit.client_ip(Req("")) == "10.0.0.1"
    finally:
        config.TRUSTED_PROXY_HOPS = saved


def test_limits_can_be_switched_off():
    ratelimit.reset()
    saved = config.RATE_LIMIT_ENABLED
    config.RATE_LIMIT_ENABLED = False
    try:
        for _ in range(50):
            ratelimit.hit("mpesa_phone", "254708374149")
    finally:
        config.RATE_LIMIT_ENABLED = saved
    ratelimit.reset()


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
