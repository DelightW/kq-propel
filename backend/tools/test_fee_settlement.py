"""
Regression tests for grounded fee settlement.

Two things must hold at once:

* A fee the passenger actually owes must be settleable over M-Pesa, including
  when Kenya Airways publishes it in US dollars - Daraja is shilling
  denominated, so the amount is converted at a declared, disclosed rate.
* The agent must never guess which of several published figures applies. Fee
  sentences almost always quote two or more ("15 US dollars on a domestic
  booking and 75 US dollars on an international booking"), and picking the
  wrong one would charge the passenger an amount no source supports.

Run: python tools/test_fee_settlement.py
"""
import sys
import traceback
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app import agent, config  # noqa: E402

NAME_FEES = ("Correcting up to three characters of a name costs 15 US dollars on a "
             "domestic booking and 75 US dollars on an international booking.")
NAME_CHUNKS = [{"section": "Section 5: Name Corrections and Name Changes"}]

PHONE_FEES = ("Booking a ticket by telephone costs from 50 US dollars, rising to "
              "100 US dollars for Business class.")
PHONE_CHUNKS = [{"section": "Section 5: Booking and Change Service Fees"}]

UNLABELLED = ("Journeys beginning at the Nairobi hub are charged 75 for Europe and "
              "25 on domestic services.")
UNLABELLED_CHUNKS = [{"section": "Section 2: Heavy Bag Fees by Region"}]

ENTITLEMENT = ("The allowance is limited to 150 US dollars for First Class and Premier "
               "World guests.")
ENTITLEMENT_CHUNKS = [{"section": "Section 2: First Needs Allowance"}]


def test_currency_is_read_from_the_source():
    amounts = agent._distinct_amounts(
        "It costs Ksh 9,000 here, USD 150 there and 75 US dollars elsewhere.")
    assert amounts == [(9000.0, "KES"), (150.0, "USD"), (75.0, "USD")], amounts


def test_unlabelled_figures_are_never_treated_as_money():
    # The published heavy-bag table carries no currency label. Inferring one
    # would put an unsupported amount in front of the passenger.
    assert agent._distinct_amounts(UNLABELLED) == []
    assert agent._payable_fee(UNLABELLED, UNLABELLED_CHUNKS, "extra bag to Europe") is None


def test_passenger_context_selects_the_matching_fee():
    assert agent._payable_fee(NAME_FEES, NAME_CHUNKS,
                               "fix a typo on my international booking") == (75.0, "USD")
    assert agent._payable_fee(NAME_FEES, NAME_CHUNKS,
                               "fix a typo on my domestic booking") == (15.0, "USD")
    assert agent._payable_fee(PHONE_FEES, PHONE_CHUNKS,
                               "I booked by telephone in Business class") == (100.0, "USD")


def test_unspecific_request_refuses_to_guess():
    assert agent._payable_fee(NAME_FEES, NAME_CHUNKS, "fix a typo on my name") is None
    assert agent._payable_fee(PHONE_FEES, PHONE_CHUNKS, "I booked by telephone") is None


def test_sentence_subject_cannot_decide_between_amounts():
    # "name" appears once in the sentence but qualifies neither figure. Scoping
    # each amount to the words that follow it keeps the subject out of the
    # comparison; a clause split would have let "name" select the 15.
    scopes = agent._amount_scopes(NAME_FEES)
    assert len(scopes) == 2
    for tokens, _ in scopes:
        assert "name" not in tokens


def test_money_owed_to_the_passenger_is_never_charged():
    assert agent._payable_fee(ENTITLEMENT, ENTITLEMENT_CHUNKS,
                               "my bag is delayed, I am in First Class") is None


def test_shilling_fee_is_not_converted():
    amount, note = agent._to_kes(9000.0, "KES")
    assert amount == 9000.0
    assert note is None


def test_dollar_fee_is_converted_and_the_rate_is_disclosed():
    amount, note = agent._to_kes(75.0, "USD")
    assert amount == round(75.0 * config.USD_TO_KES_RATE)
    assert note is not None
    assert "USD 75" in note
    assert f"{config.USD_TO_KES_RATE:,.2f}" in note
    # The passenger must be told the figure is the assistant's, not the airline's.
    assert "not a live exchange rate" in note
    assert "does not publish" in note


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except Exception:
            failures += 1
            print(f"  FAIL  {test.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
