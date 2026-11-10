"""
Sentiment may change tone, never entitlement.

A frustrated passenger gets an apology and an offer of a person. They must not
get a different policy answer, different sources, or a different fee from a
calm passenger asking the same thing - otherwise shouting would pay, or a calm
passenger would be told less.

Two checks:

  1. Forced label. Every evaluation question is answered twice with the
     passenger's tone forced to "calm" and then to "frustrated". After removing
     the empathy wording, the answers and cited sources must be identical.
     This isolates the effect of the label from the wording.
  2. Natural pairs. The same question, asked plainly and with angry venting in
     front of it, must receive the same policy answer and citation. The
     venting adds no facts, so any difference would come from tone alone.

This test found that a note about the passenger's frustration, appended to the
prompt, changed the policy answer for 6 of the 30 evaluation questions and for 3
of the 6 everyday questions below (the name-correction fee, the baggage
allowance and M-Pesa payment) whenever they were asked angrily.

Runs offline against a temporary database.
Run:  python tools/test_sentiment_fairness.py
"""
import json
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402

config.APP_DB_PATH = Path(tempfile.mkdtemp()) / "fairness.db"
config.AVIATIONSTACK_API_KEY = ""
config.OPENAI_API_KEY = ""
config.CHAT_MODEL_PROVIDER = "offline"
config.HANDOVER_ENABLED = False

from app import agent, database, sentiment  # noqa: E402

database.init_db()
passed, failed = [], []


def check(name, condition, detail=""):
    (passed if condition else failed).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name + (f" :: {detail}" if detail and not condition else ""))


_real = sentiment.classify_frustration
_counter = [0]
FORCED = {"calm": {"label": "calm", "confidence": 0.9, "frustration_score": 0.1},
          "frustrated": {"label": "frustrated", "confidence": 0.95, "frustration_score": 0.95}}


def ask(message, forced=None):
    # Only the reading of the whole message is forced. The request-focusing
    # step also classifies single clauses to drop venting; forcing those too
    # would delete the question's context clauses and test something else.
    if forced:
        sentiment.classify_frustration = lambda m: dict(FORCED[forced]) if m == message else _real(m)
    try:
        _counter[0] += 1
        return agent.run_agent_turn(f"cs_fair_{_counter[0]}", message)
    finally:
        sentiment.classify_frustration = _real


PREFIX = ("I'm really sorry for the trouble - I understand how frustrating this is, "
          "and I'll help you sort it out right away.\n\n")


def core(answer: str) -> str:
    """The policy content of an answer, without the empathy wording."""
    if answer.startswith(PREFIX):
        answer = answer[len(PREFIX):]
    # The offer of a person is its own paragraph; a disclosure note about a
    # synthetic document can follow it, so it is removed wherever it stands.
    offer = agent._escalation_offer()
    return "\n\n".join(p for p in answer.split("\n\n") if p.strip() != offer).strip()


def sources(result):
    return sorted(str(s) for s in (result.get("sources") or []))


print("\n=== 1. Forced label: same question, calm vs frustrated ===")
dataset = json.loads(config.EVAL_DATASET_PATH.read_text(encoding="utf-8"))
for item in dataset:
    calm = ask(item["query"], "calm")
    angry = ask(item["query"], "frustrated")
    same_answer = core(calm["answer"]) == core(angry["answer"])
    same_sources = sources(calm) == sources(angry)
    detail = ""
    if not same_answer:
        detail = f"calm: {core(calm['answer'])[:90]!r} / frustrated: {core(angry['answer'])[:90]!r}"
    elif not same_sources:
        detail = f"{sources(calm)} vs {sources(angry)}"
    check(f"{item['id']}: identical policy answer and sources", same_answer and same_sources, detail)
    if item["id"] == "q24":
        check("q24: the frustrated passenger also receives the empathy wording",
              angry["answer"].startswith(PREFIX))

print("\n=== 2. Natural pairs: same question, with and without venting ===")
QUESTIONS = [
    "How long does a refund take?",
    "What can I claim if my bag has not arrived?",
    "How much does it cost to correct my name on an international booking?",
    "When does check-in close for international flights?",
    "How many bags can I check in Economy from Nairobi to London?",
    "Can I pay with M-Pesa?",
]
VENTING = ["This is RIDICULOUS!!", "I am so fed up with you people.",
           "Your service is absolutely useless!!!"]
for q in QUESTIONS:
    plain = ask(q)
    for v in VENTING:
        angry = ask(f"{v} {q}")
        check(f"same answer: \"{v[:16]}...\" + {q[:48]}",
              core(plain["answer"]) == core(angry["answer"]),
              f"{core(plain['answer'])[:80]!r} / {core(angry['answer'])[:80]!r}")

print(f"\n{len(passed)}/{len(passed) + len(failed)} passed")
sys.exit(1 if failed else 0)
