"""Baggage questions are answered from the right policy (defect D-19).

"What is the baggage allowance for economy class?" - the first suggestion in
the chat - was answered with the cabin-bag rule, and "What is the checked
baggage allowance in Economy class?" did not retrieve the checked allowance at
all. Retrieval now tells checked, hand, delayed and special baggage apart.

Run:  python tools/test_baggage_questions.py
"""
import sys
import tempfile
import traceback
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402

config.APP_DB_PATH = Path(tempfile.mkdtemp()) / "bags.db"
config.OPENAI_API_KEY = ""
config.CHAT_MODEL_PROVIDER = "offline"
config.AVIATIONSTACK_API_KEY = ""
config.KQ_BOOKING_API_URL = ""

from app import agent, database, retrieval  # noqa: E402

database.init_db()
_n = [0]


def ask(question):
    _n[0] += 1
    return agent.run_agent_turn(f"cs_bags_{_n[0]}", question)


def test_which_bag_is_meant():
    cases = {
        "What is the baggage allowance for economy class?": "checked",
        "What is the checked baggage allowance in Economy class?": "checked",
        "How many bags can I check in Business class?": "checked",
        "What is the cabin baggage allowance?": "hand",
        "How big can my carry-on be?": "hand",
        "Can I take a laptop bag in the Business cabin?": "hand",
        "My bag has not arrived, what allowance can I claim?": "delayed",
        "Can I bring my golf clubs?": "special",
        # Both kinds named: neither policy may be pushed down.
        "I am flying Business to London with two checked bags and two cabin bags. Is all of that "
        "within my allowance?": None,
        # "cabin" alone is a travel class, and check-in is not a bag.
        "When does check-in close?": None,
        "Which cabin is best for a long flight?": None,
    }
    for question, expected in cases.items():
        assert retrieval.baggage_kind(question) == expected, (question, retrieval.baggage_kind(question))


def test_allowance_questions_get_the_checked_allowance():
    for question in ("What is the baggage allowance for economy class?",
                     "What is the checked baggage allowance in Economy class?",
                     "What is the baggage allowance for Business class?"):
        answer = ask(question)["answer"]
        assert answer.startswith("Kenya Airways applies the piece concept to checked baggage"), (question, answer[:160])
        assert "two pieces of up to 23kg" in answer, question
        assert "cabin piece" not in answer and "First Needs" not in answer and "ski" not in answer, question


def test_hand_and_delayed_questions_keep_their_policies():
    hand = ask("What is the hand baggage allowance in Economy?")["answer"]
    assert hand.startswith("Economy guests may bring one cabin piece plus one accessory"), hand[:160]
    size = ask("How big can my carry-on bag be?")["answer"]
    assert "55cm" in size, size[:160]
    delayed = ask("My bag has not arrived, what allowance can I claim?")["answer"]
    assert "First Needs Allowance" in delayed, delayed[:160]


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
