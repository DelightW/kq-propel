"""
Paying for an extra bag (app/extra_bag.py): the fee comes from the policy's
heavy-bag table for the booked route, the missing currency is stated, and the
amount never comes from the passenger's message.

Payments are not sent anywhere (no gateway credentials). Temporary database.
Run:  python tools/test_extra_bag.py
"""
import sys
import tempfile
import traceback
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402

config.APP_DB_PATH = Path(tempfile.mkdtemp()) / "extra_bag.db"
config.AVIATIONSTACK_API_KEY = ""
config.OPENAI_API_KEY = ""
config.CHAT_MODEL_PROVIDER = "offline"
config.KQ_BOOKING_API_URL = ""

from app import agent, database, extra_bag  # noqa: E402

database.init_db()
_n = [0]


def session():
    _n[0] += 1
    return f"cs_bag_{_n[0]}"


def say(sid, message):
    return agent.run_agent_turn(sid, message)["answer"]


def test_fees_are_read_from_the_policy_table():
    text = open(config.POLICY_DIR / "excess_baggage_fees_policy.txt", encoding="utf-8").read()
    table = extra_bag.fees([{"text": text, "section": "Section 2: Heavy Bag Fees by Region",
                             "source": "excess_baggage_fees_policy.txt"}])
    assert table["hub"] == {"europe": 75, "americas": 75, "asia": 50, "middle east": 50,
                            "africa": 50, "domestic": 25}, table["hub"]
    assert table["other"]["europe"] == 100 and table["other"]["africa"] == 60
    assert extra_bag.fees([{"text": "nothing about bags"}]) is None


def test_the_intent_is_recognised():
    for yes in ("I want to pay for an extra bag", "can I buy an additional bag?",
                "I need to pay for excess baggage", "add another suitcase please",
                "can i pay for an added baggage", "I want to pay for extra kg",
                "can I pay for a heavier bag", "I need to pay for a bag that is overweight",
                "pay for an additional checked bag", "How much to add 1 bag of 10kg",
                "can I buy two more bags"):
        assert extra_bag.wants_extra_bag(yes), yes
    # A price question becomes an offer only when a booking gives the route.
    for price in ("how much is an extra bag?", "what does an additional suitcase cost"):
        assert extra_bag.wants_extra_bag(price, has_booking=True), price
        assert not extra_bag.wants_extra_bag(price), price
    assert not extra_bag.wants_extra_bag("how much is an extra bag to Europe from Nairobi?", has_booking=True)
    for no in ("how many bags can I check?", "my bag is delayed", "what is the hand luggage limit?",
               "I am a Gold member of Asante Rewards. Do I get any extra baggage?",
               "Do I need to pay for an extra bag if I'm entitled to a free one?"):
        assert not extra_bag.wants_extra_bag(no), no


def test_without_a_booking_it_asks_and_then_quotes_the_route():
    s = session()
    first = say(s, "I want to pay for an extra bag")
    assert "booking reference" in first and database.get_pending_payment(s) is None
    found = say(s, "booking KQDLW1, surname Wangui")
    assert "sample data" in found
    assert "heavy bag fee for Europe" in found and "USD 75.00" in found, found
    assert "without a currency label" in found, "the currency assumption must be stated"
    pending = database.get_pending_payment(s)
    assert float(pending["amount"]) == 75.0 and pending["currency"] == "USD", pending
    assert "KQDLW1" in pending["description"]


def test_domestic_and_african_routes():
    for lookup, fee in (("booking KQNC01, surname Kanja", "USD 25.00"),
                        ("booking KQNC03, surname Wambui", "USD 50.00")):
        s = session()
        say(s, lookup)
        offer = say(s, "I need to pay for an extra bag")
        assert fee in offer, (lookup, offer)


def test_how_much_to_add_a_bag_on_a_booking_is_quoted():
    s = session()
    say(s, "booking KQNC01, surname Kanja")
    offer = say(s, "How much to add 1 bag of 10kg")
    assert "USD 25.00" in offer and "Card" in offer, offer


def test_a_typed_amount_is_ignored():
    s = session()
    say(s, "booking KQDEMO, surname Wanjiru")
    offer = say(s, "I want to pay for an extra bag, charge me USD 1")
    assert "USD 75.00" in offer
    assert float(database.get_pending_payment(s)["amount"]) == 75.0


def test_the_offer_continues_to_payment():
    s = session()
    say(s, "booking KQDEMO, surname Wanjiru")
    say(s, "I want to pay for an extra bag")
    assert "USD 75.00" in say(s, "card")


def test_a_fee_question_is_still_answered_from_policy():
    s = session()
    answer = say(s, "how are excess baggage fees charged?")
    assert "flat heavy bag fee" in answer and database.get_pending_payment(s) is None


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
