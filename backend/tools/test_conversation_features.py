"""Conversation memory, human handover and the live-model fallback.

Runs against a temporary database with a fake model. It never contacts OpenAI,
Ollama, AviationStack or a contact-centre webhook, and never touches
backend/data/kq_propel.db.

Run:  python tools/test_conversation_features.py
"""
import sys
import tempfile
import traceback
from datetime import datetime, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402

config.APP_DB_PATH = Path(tempfile.mkdtemp()) / "conversation.db"
config.AVIATIONSTACK_API_KEY = ""
config.OPENAI_API_KEY = ""
config.CHAT_MODEL_PROVIDER = "auto"
config.HANDOVER_ENABLED = True
config.HANDOVER_WEBHOOK_URL = ""
config.ADMIN_PASSWORD_HASH = ""
config.ADMIN_PASSWORD = "test-staff-password"

from app import agent, database, handover, llm, memory  # noqa: E402

database.init_db()


def turn(session, message):
    return agent.run_agent_turn(session, message)


def searched_for(result):
    """The query the retrieval step actually ran."""
    return next(s["input"] for s in result["trace"]
                if s.get("tool") == "retrieve_policy_documents")


# ------------------------------------------------------------------ memory

def test_follow_up_detection():
    for msg in ["What about Business class?", "And to Dubai?", "Business class?",
                "same for Paris", "what if I'm flying from Mombasa?"]:
        assert memory.is_follow_up(msg), msg
    for msg in ["Is it allowed to carry a power bank?", "How much is the overweight fee?",
                "My bag never arrived in Amsterdam. What can I claim?"]:
        assert not memory.is_follow_up(msg), msg


def test_merge_replaces_only_what_changed():
    prev = "How many bags can I check in Economy on a flight from Nairobi to London?"
    merged = memory.merge(prev, "What about Business class?")
    assert "Economy" not in merged and "Business class" in merged, merged
    assert "Nairobi" in merged and "London" in merged, merged
    merged = memory.merge(prev, "And to Dubai?")
    assert "London" not in merged and "Dubai" in merged and "Nairobi" in merged, merged
    assert "Economy" in merged, merged
    merged = memory.merge(prev, "what about from Mombasa?")
    assert "Nairobi" not in merged and "Mombasa" in merged and "London" in merged, merged


def test_follow_up_is_answered_in_context():
    s = "mem-1"
    first = turn(s, "How many bags can I check in Economy from Nairobi to London?")
    assert first["sources"], first["answer"]
    second = turn(s, "What about Business class?")
    query = searched_for(second)
    assert query == "How many bags can I check in Business class from Nairobi to London?", query
    # The answer itself, not just the search: an earlier merge searched for
    # the right words and still answered with an excess-baggage fee.
    assert "Business guests may check two pieces of up to 32kg" in second["answer"], second["answer"]


def test_follow_up_without_history_is_not_rewritten():
    result = turn("mem-fresh", "What about Business class?")
    assert searched_for(result) == "What about Business class?"


def test_memory_expires():
    s = "mem-stale"
    memory.remember(s, query="What is the baggage allowance?")
    old = (datetime.utcnow() - timedelta(minutes=config.CONVERSATION_MEMORY_MINUTES + 1))
    with database._connect() as conn:
        conn.execute("UPDATE conversation_context SET updated_at = ? WHERE session_id = ?",
                     (old.isoformat(), s))
    assert memory.resolve_query(s, "What about Business class?") is None


def test_that_flight_uses_the_flight_named_earlier():
    s = "mem-flight"
    turn(s, "What is the status of KQ100?")
    result = turn(s, "Is that flight delayed?")
    calls = [step for step in result["trace"] if step.get("tool") == "get_flight_status"]
    assert calls and calls[0]["input"] == "KQ100", result["trace"]


def test_sessions_do_not_share_memory():
    turn("mem-a", "How many bags can I check in Economy on a flight from Nairobi to London?")
    assert memory.resolve_query("mem-b", "What about Business class?") is None


# ---------------------------------------------------------------- handover

def test_request_opens_one_case():
    s = "ho-1"
    first = turn(s, "I want to speak to a real person")
    assert first["handover"] and first["handover"]["status"] == "open", first
    assert "H-" in first["answer"]
    again = turn(s, "escalate")
    assert again["handover"]["reference"] == first["handover"]["reference"]
    assert len([c for c in handover.list_cases() if c["session_id"] == s]) == 1


def test_assistant_stays_quiet_while_staff_own_the_case():
    s = "ho-quiet"
    turn(s, "connect me to an agent")
    result = turn(s, "How many bags can I check in Economy?")
    assert result["sources"] == [] and "added that to your case" in result["answer"], result


def test_staff_reply_reaches_only_that_passenger():
    s = "ho-reply"
    case = turn(s, "talk to a human please")["handover"]
    case_id = int(case["reference"].split("-")[1])
    handover.staff_reply(case_id, "agent.wanjiru", "Hello, I'm looking at your booking now.")
    msgs = handover.messages_since(s, 0)
    assert [m["message"] for m in msgs] == ["Hello, I'm looking at your booking now."]
    assert handover.get(case_id)["status"] == handover.CLAIMED
    assert handover.messages_since("someone-else", 0) == []
    assert handover.messages_since(s, msgs[-1]["id"]) == []


def test_passenger_can_return_to_the_assistant():
    s = "ho-back"
    turn(s, "escalate")
    back = turn(s, "back to assistant")
    assert back["handover"] is None and handover.active(s) is None, back
    answered = turn(s, "How many bags can I check in Economy on a flight from Nairobi to London?")
    assert answered["sources"], answered["answer"]


def test_disabled_handover_promises_no_one():
    config.HANDOVER_ENABLED = False
    try:
        result = turn("ho-off", "I want to speak to a real person")
        frustrated = agent._apply_empathy("Answer.", {"label": "frustrated",
                                                      "frustration_score": 0.9})
    finally:
        config.HANDOVER_ENABLED = True
    assert result["handover"] is None and handover.active("ho-off") is None
    assert config.HANDOVER_FALLBACK_CONTACT in result["answer"], result["answer"]
    assert "escalate" not in frustrated.lower() and "pass you" not in frustrated, frustrated
    assert config.HANDOVER_FALLBACK_CONTACT in frustrated


def test_airport_staff_question_is_not_a_handover():
    assert not handover.is_request("Do I need to speak to an agent at the counter?")


def test_webhook_failure_is_recorded_not_raised():
    saved = config.HANDOVER_WEBHOOK_URL, handover.requests.post

    def boom(*a, **k):
        raise handover.requests.ConnectionError("down")

    config.HANDOVER_WEBHOOK_URL, handover.requests.post = "https://contact-centre.invalid/hook", boom
    try:
        case = handover.open_case("ho-hook", "escalate")
    finally:
        config.HANDOVER_WEBHOOK_URL, handover.requests.post = saved
    assert case["status"] == "open" and case["forwarded"].startswith("failed"), case


def test_http_handover_flow():
    from fastapi.testclient import TestClient
    from app.main import app as fastapi_app

    passenger = TestClient(fastapi_app)
    sid = passenger.post("/api/chat/session").json()["session_id"]
    opened = passenger.post("/api/chat", json={"message": "I need to speak to a person"}).json()
    ref = opened["handover"]["reference"]
    case_id = int(ref.split("-")[1])

    staff = TestClient(fastapi_app)
    assert staff.get("/api/admin/handovers").status_code == 401
    assert staff.post("/api/admin/login", json={"username": config.ADMIN_USERNAME,
                                                "password": config.ADMIN_PASSWORD}).status_code == 200
    queue = staff.get("/api/admin/handovers").json()
    assert any(c["reference"] == ref for c in queue["cases"]), queue
    detail = staff.get(f"/api/admin/handovers/{case_id}").json()
    assert any(m["role"] == "user" for m in detail["transcript"]), detail
    assert staff.post(f"/api/admin/handovers/{case_id}/reply",
                      json={"message": "Hi, this is Achieng from customer care."}).status_code == 200

    update = passenger.get("/api/chat/handover?after=0").json()
    assert update["handover"]["status"] == "claimed"
    assert update["messages"][0]["message"] == "Hi, this is Achieng from customer care."

    stranger = TestClient(fastapi_app)
    stranger.post("/api/chat/session")
    assert stranger.get("/api/chat/handover?after=0").json() == {"handover": None, "messages": []}

    assert staff.post(f"/api/admin/handovers/{case_id}/close",
                      json={"note": "Resolved - safe travels."}).status_code == 200
    update = passenger.get(f"/api/chat/handover?after={update['messages'][0]['id']}").json()
    assert update["handover"] is None
    assert update["messages"][0]["message"] == "Resolved - safe travels."
    assert staff.post(f"/api/admin/handovers/{case_id}/reply",
                      json={"message": "late"}).status_code == 409
    audit = [e["action"] for e in staff.get("/api/admin/audit").json()["entries"]]
    assert {"view_handover", "reply_handover", "close_handover"} <= set(audit), audit
    assert sid  # the passenger kept one session throughout


def test_reset_clears_memory_and_handover():
    from fastapi.testclient import TestClient
    from app.main import app as fastapi_app

    client = TestClient(fastapi_app)
    sid = client.post("/api/chat/session").json()["session_id"]
    client.post("/api/chat", json={"message": "How many bags can I check in Economy to London?"})
    client.post("/api/chat", json={"message": "escalate"})
    assert memory.recall(sid) and handover.active(sid)
    client.post("/api/chat/reset")
    assert memory.recall(sid) == {} and handover.active(sid) is None


# ------------------------------------------------------------ model fallback

class _FakeLive(llm.BaseLLM):
    name = "fake-live"
    is_simulated = False

    def __init__(self, reply=None, error=None):
        self.reply, self.error = reply, error

    def generate(self, system_prompt, user_prompt):
        if self.error:
            raise self.error
        return self.reply


def _with_model(model, fn):
    saved = llm.get_primary_llm
    llm.get_primary_llm = lambda: model
    try:
        return fn()
    finally:
        llm.get_primary_llm = saved


PROMPT = ("CONTEXT:\n[Excess Baggage Fees Policy | Section 2: Fees]\nAn extra bag to Europe "
          "costs 75 US dollars.\n\nTOOL OBSERVATIONS:\n\nQUESTION:\nHow much is an extra bag "
          "to Europe?")


def test_live_answer_is_used_when_it_checks_out():
    out = _with_model(_FakeLive("An extra bag to Europe costs 75 US dollars."),
                      lambda: llm.generate_answer(agent.SYSTEM_PROMPT, PROMPT))
    assert out["model"] == "fake-live" and out["fallback"] is None, out


def test_invented_figure_falls_back_to_the_composer():
    out = _with_model(_FakeLive("An extra bag to Europe costs 95 US dollars."),
                      lambda: llm.generate_answer(agent.SYSTEM_PROMPT, PROMPT))
    assert out["fallback"] and "95" in out["fallback"], out
    assert "75" in out["answer"] and "95" not in out["answer"], out


def test_outage_falls_back_to_the_composer():
    out = _with_model(_FakeLive(error=TimeoutError("slow")),
                      lambda: llm.generate_answer(agent.SYSTEM_PROMPT, PROMPT))
    assert out["fallback"] and "TimeoutError" in out["fallback"], out
    assert "75" in out["answer"], out


def test_fallback_is_visible_in_the_trace():
    result = _with_model(_FakeLive(error=ConnectionError("down")),
                         lambda: turn("fb-1", "How many bags can I check in Economy on a "
                                              "flight from Nairobi to London?"))
    assert result["model_used"].startswith("extractive"), result["model_used"]
    assert any("unavailable" in str(s.get("content", "")) for s in result["trace"])


def test_provider_setting_selects_the_model():
    saved = config.CHAT_MODEL_PROVIDER, config.OPENAI_API_KEY
    try:
        config.CHAT_MODEL_PROVIDER = "ollama"
        assert isinstance(llm.get_primary_llm(), llm.OllamaLLM)
        config.CHAT_MODEL_PROVIDER, config.OPENAI_API_KEY = "openai", ""
        assert llm.get_primary_llm().is_simulated  # no key: never a broken client
        config.CHAT_MODEL_PROVIDER, config.OPENAI_API_KEY = "offline", "sk-test"
        assert llm.get_primary_llm().is_simulated
    finally:
        config.CHAT_MODEL_PROVIDER, config.OPENAI_API_KEY = saved


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
