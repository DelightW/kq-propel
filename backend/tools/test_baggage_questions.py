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


def test_one_piece_route_is_not_answered_with_excess_charges():
    # Known issue: the Mumbai allowance began with an excess-baggage sentence.
    answer = ask("How many bags can I check from Nairobi to Mumbai?")["answer"]
    assert answer.startswith("Kenya Airways applies the piece concept"), answer[:160]
    assert "one piece of up to 23kg" in answer and "per-kilogram" not in answer, answer[:300]
    # Asking about a fee still reaches the excess-baggage policy.
    fee = ask("How much does an extra bag cost from Nairobi to Mumbai?")["answer"]
    assert "heavy bag fee" in fee, fee[:200]


def test_a_delayed_bag_is_not_a_delayed_flight():
    # Known issue: "my bag is delayed 4 hours" was answered from the flight-delay bands.
    bag = ask("My bag is delayed 4 hours, what can I get?")["answer"]
    assert "First Needs Allowance" in bag and "meal voucher" not in bag, bag[:200]
    # And a delayed flight is matched to its band, not to the over-eight-hours refund rule.
    flight = ask("My flight is delayed 4 hours, what can I get?")["answer"]
    assert "four to eight hours" in flight and "more than eight hours may claim" not in flight, flight[:200]
    # A message about both keeps both documents (evaluation question q26).
    both = ask("My flight is delayed four hours and my bag is one piece over the allowance to Europe. "
               "What am I owed and what do I owe?")
    sources = " ".join(both["sources"])
    assert "Delay Care" in sources or "Delay" in sources, sources


def test_golf_clubs_get_the_sporting_equipment_rates():
    answer = ask("Can I bring my golf clubs?")["answer"]
    assert "Golf equipment" in answer and "First Needs" not in answer, answer[:200]


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
