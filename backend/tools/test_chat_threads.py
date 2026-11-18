"""
A browser's chat list (routers/chat.py, /api/chat/threads) and the reasoning
steps kept for staff review.

A browser can list, open, start, pin and delete only its own chats; deleting
erases the chat. Each answer's reasoning is saved with it and shown to staff
with phone numbers masked.

Temporary database; nothing contacts Safaricom.
Run:  python tools/test_chat_threads.py
"""
import sys
import tempfile
import traceback
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402

config.APP_DB_PATH = Path(tempfile.mkdtemp()) / "threads.db"
config.AVIATIONSTACK_API_KEY = ""
config.OPENAI_API_KEY = ""
config.CHAT_MODEL_PROVIDER = "offline"
config.KQ_BOOKING_API_URL = ""
config.RATE_LIMIT_ENABLED = False
config.ADMIN_PASSWORD_HASH = ""
config.ADMIN_PASSWORD = "test-staff-password"

from fastapi.testclient import TestClient  # noqa: E402

from app import database, review  # noqa: E402
from app.main import app  # noqa: E402

database.init_db()


def browser():
    c = TestClient(app)
    c.post("/api/chat/session")
    return c


def ask(c, text):
    res = c.post("/api/chat", json={"message": text})
    assert res.status_code == 200, res.text
    return res.json()


def threads(c):
    return c.get("/api/chat/threads").json()


def test_chats_are_listed_newest_first_with_their_first_question():
    c = browser()
    assert threads(c)["threads"] == [], "an empty chat is not listed"
    ask(c, "How many bags can I check in Economy to London?")
    ask(c, "What about Business class?")
    first = threads(c)
    assert [t["title"] for t in first["threads"]] == ["How many bags can I check in Economy to London?"]
    assert first["active"] == first["threads"][0]["id"]
    new = c.post("/api/chat/threads/new").json()
    assert new["thread_id"] != first["active"]
    again = c.post("/api/chat/threads/new").json()
    assert again["thread_id"] == new["thread_id"], "an empty new chat is reused"
    ask(c, "When does check-in close?")
    listed = threads(c)
    assert [t["title"] for t in listed["threads"]] == [
        "When does check-in close?", "How many bags can I check in Economy to London?"]
    assert listed["active"] == new["thread_id"]


def test_opening_a_chat_switches_the_conversation():
    c = browser()
    ask(c, "When does check-in close for international flights?")
    old = threads(c)["active"]
    c.post("/api/chat/threads/new")
    ask(c, "My bag didn't arrive. What can I claim?")
    opened = c.post(f"/api/chat/threads/{old}/open").json()
    assert opened["thread_id"] == old
    history = [m["message"] for m in c.get("/api/chat/history").json()["messages"] if m["role"] == "user"]
    assert history == ["When does check-in close for international flights?"]
    assert threads(c)["active"] == old


def test_pinned_chats_come_first():
    c = browser()
    ask(c, "first chat question")
    first = threads(c)["active"]
    c.post("/api/chat/threads/new")
    ask(c, "second chat question")
    assert threads(c)["threads"][0]["id"] != first
    assert c.post(f"/api/chat/threads/{first}/pin", json={"pinned": True}).json()["pinned"] is True
    listed = threads(c)["threads"]
    assert listed[0]["id"] == first and listed[0]["pinned"] and not listed[1]["pinned"]
    c.post(f"/api/chat/threads/{first}/pin", json={"pinned": False})
    assert not any(t["pinned"] for t in threads(c)["threads"])


def test_deleting_a_chat_erases_it():
    c = browser()
    ask(c, "a chat to keep")
    keep = threads(c)["active"]
    c.post("/api/chat/threads/new")
    ask(c, "a chat to delete")
    doomed = threads(c)["active"]
    with database._connect() as conn:
        doomed_session = conn.execute("SELECT session_id FROM chat_sessions WHERE thread_id = ?",
                                      (doomed,)).fetchone()[0]
    assert c.post(f"/api/chat/threads/{doomed}/delete", json={"confirm": False}).status_code == 400
    res = c.post(f"/api/chat/threads/{doomed}/delete", json={"confirm": True}).json()
    assert res["erased"]["messages"] >= 2
    assert database.fetch_conversation(doomed_session) == []
    listed = threads(c)
    assert [t["id"] for t in listed["threads"]] == [keep]
    assert listed["active"] == res["thread_id"] and res["thread_id"] not in (keep, doomed), \
        "deleting the open chat starts a new one"
    assert c.get("/api/chat/history").json()["messages"] == []


def test_another_browser_cannot_reach_these_chats():
    mine, theirs = browser(), browser()
    ask(mine, "my private question")
    tid = threads(mine)["active"]
    assert threads(theirs)["threads"] == []
    assert theirs.post(f"/api/chat/threads/{tid}/open").status_code == 404
    assert theirs.post(f"/api/chat/threads/{tid}/pin", json={"pinned": True}).status_code == 404
    assert theirs.post(f"/api/chat/threads/{tid}/delete", json={"confirm": True}).status_code == 404
    assert [t["title"] for t in threads(mine)["threads"]] == ["my private question"]
    # The session id itself never reaches the page.
    assert "cs_" not in mine.get("/api/chat/threads").text


def test_an_invented_owner_cookie_finds_nothing():
    mine = browser()
    ask(mine, "question under my owner")
    stranger = TestClient(app)
    stranger.cookies.set("kq_owner", "x" * 40)
    stranger.post("/api/chat/session")
    assert threads(stranger)["threads"] == []


def test_a_chat_from_before_threads_is_adopted():
    c = TestClient(app)
    database.create_chat_session("cs_legacy_one", "127.0.0.1")      # no owner, as before
    database.log_message("cs_legacy_one", "user", "an older question")
    c.cookies.set("kq_session", "cs_legacy_one")
    assert c.post("/api/chat/session").json()["resumed"] is True
    assert [t["title"] for t in threads(c)["threads"]] == ["an older question"]


def test_reasoning_is_saved_for_staff_with_numbers_masked():
    c = browser()
    data = ask(c, "How many bags can I check in Economy to London?")
    assert data["trace"], "the answer still carries its trace in the response"
    session = database.get_chat_session(c.cookies.get("kq_session"))["session_id"]
    detail = review.conversation_detail(session)
    answer = [m for m in detail["messages"] if m["role"] == "assistant"][0]
    kinds = [s["step"] for s in answer["trace"]]
    assert "thought" in kinds and "final_answer" in kinds, kinds
    assert any(s.get("tool") == "retrieve_policy_documents" for s in answer["trace"])
    database.save_trace(answer["id"], '[{"step": "thought", "content": "pushing to 0708374149, '
                                      'checkout ws_CO_11112026124017474708374149"}]')
    shown = review.conversation_detail(session)["messages"][1]["trace"][0]["content"]
    assert "0708374149" not in shown and "474708374149" not in shown, shown


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
