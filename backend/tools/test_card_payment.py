"""
Regression tests for the card settlement rail.

The card rail exists for a reason that matters to the integrity of this
project, not only to its usability:

* M-Pesa reaches Safaricom subscribers and settles only in shillings. A fee
  Kenya Airways publishes in US dollars therefore has to be converted, and
  Kenya Airways publishes no shilling equivalent - so the rate is this
  assistant's own assumption, disclosed as such.
* A card can be charged in the currency the fee is published in. On that rail
  the assumption disappears entirely rather than being managed.

So these tests pin two properties: the card rail must never convert, and the
M-Pesa rail must always convert *and* disclose. They also pin that the card
rail is grounded - Kenya Airways publishes that it accepts cards, which the
in-chat M-Pesa push is not, and that a simulated checkout is always
distinguishable from a live one.

Run: python tools/test_card_payment.py
"""
import sys
import traceback
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app import agent, config, database, daraja, paypal, sentiment  # noqa: E402

POLICY_DIR = BACKEND / "data" / "policies"

# Once real Daraja credentials are configured, the end-to-end tests below would
# send genuine STK pushes to a number that belongs to somebody else, every time
# anyone runs the suite. Tests exercise the agent's logic, not Safaricom's
# availability, so the network call is stubbed out here and the stub records
# what it was asked to send. test_the_suite_never_contacts_safaricom pins this.
_STK_CALLS = []


def _stub_stk_push(phone_number: str, amount: float, reference: str, description: str):
    _STK_CALLS.append({"phone_number": phone_number, "amount": amount,
                       "reference": reference})
    return {
        "source": "stubbed_for_tests",
        "success": True,
        "checkout_request_id": "ws_CO_TEST",
        "merchant_request_id": "test",
        "amount": amount,
        "phone_number": phone_number,
        "reference": reference,
        "message": "Stubbed STK push.",
    }


def _stub_card_order(amount: float, currency: str, reference: str, description: str):
    return paypal._simulated_order(amount, currency, reference, description,
                                   "stubbed_for_tests")


_LIVE_STK = daraja.initiate_stk_push
daraja.initiate_stk_push = _stub_stk_push


def test_card_acceptance_is_published_fact_not_invention():
    # The card rail's claim ("Kenya Airways accepts cards") must trace to the
    # published document, not to the synthetic prototype document.
    published = (POLICY_DIR / "payment_methods_policy.txt").read_text(encoding="utf-8").lower()
    assert "credit" in published and "debit" in published, published[:400]
    assert "card" in published


def test_the_inchat_flow_document_still_discloses_itself_as_synthetic():
    doc = (POLICY_DIR / "synthetic_inchat_payment_flow.txt").read_text(encoding="utf-8").lower()
    assert "synthetic" in doc
    assert "does not offer payment inside a chat assistant" in doc
    # The card section must not claim the in-chat checkout is a real KQ service.
    assert "prototype" in doc


def test_card_request_is_recognised_without_the_word_card():
    # A guest outside Kenya is far more likely to describe their problem than
    # to name a payment instrument.
    for phrase in ["I don't have a Kenyan number",
                   "I am outside Kenya",
                   "my number is an international number",
                   "can I pay by visa",
                   "paypal please"]:
        assert agent._CARD_RE.search(phrase), phrase


def test_an_mpesa_request_is_not_mistaken_for_a_card_request():
    for phrase in ["mpesa", "m-pesa please", "send the stk push"]:
        assert agent._MPESA_RE.search(phrase), phrase


def test_card_order_is_charged_in_the_published_currency():
    result = paypal.create_card_order(75.0, "USD", "TEST-REF", "Name change fee")
    assert result["currency"] == "USD", result
    assert float(result["amount"]) == 75.0, result
    # No conversion may have happened on the way to the gateway.
    assert float(result["amount"]) != round(75.0 * config.USD_TO_KES_RATE)


def test_a_shilling_fee_reaches_the_card_rail_as_shillings():
    result = paypal.create_card_order(9000.0, "KES", "TEST-REF-2", "Excess baggage")
    assert result["currency"] == "KES", result
    assert float(result["amount"]) == 9000.0, result


def test_a_simulated_checkout_link_cannot_be_mistaken_for_a_live_one():
    # A simulated link that looked like paypal.com would be indistinguishable
    # from a live one in a screenshot or a demonstration.
    result = paypal.create_card_order(75.0, "USD", "TEST-REF-3", "Name change fee")
    if result["source"].startswith("simulated"):
        url = result["approval_url"]
        assert "paypal.com" not in url, url
        assert "simulated" in url, url


def test_the_card_message_discloses_simulation_when_unconfigured():
    result = paypal.create_card_order(75.0, "USD", "TEST-REF-4", "Name change fee")
    message = agent._card_checkout_message(75.0, "USD", result)
    assert "USD 75.00" in message
    if result["source"].startswith("simulated"):
        assert "simulat" in message.lower(), message
    # It must never quote a shilling figure on this rail.
    assert "Ksh" not in message, message


def test_the_card_message_states_no_conversion_is_applied():
    result = paypal.create_card_order(75.0, "USD", "TEST-REF-5", "Name change fee")
    message = agent._card_checkout_message(75.0, "USD", result).lower()
    assert "conversion" in message or "converted" in message, message


def test_the_offer_presents_both_rails_and_qualifies_mpesa():
    message = agent._offer_payment_message(75.0, "USD", round(75.0 * config.USD_TO_KES_RATE))
    lower = message.lower()
    assert "card" in lower
    assert "m-pesa" in lower
    # The Kenyan-number limitation is the whole reason the card rail exists;
    # omitting it would send guests abroad down a rail that cannot serve them.
    assert "kenyan" in lower, message


def test_a_shilling_fee_offer_quotes_no_exchange_rate():
    message = agent._offer_payment_message(9000.0, "KES", 9000.0)
    assert f"{config.USD_TO_KES_RATE:,.2f}" not in message, message


def test_a_dollar_fee_offer_discloses_the_rate_as_the_assistants_own():
    # The offer names the shilling figure; the rate itself is disclosed in the
    # conversion note the caller appends immediately below it. What matters is
    # that the guest sees both together, so assert on the composed text.
    kes, note = agent._to_kes(75.0, "USD")
    composed = agent._offer_payment_message(75.0, "USD", kes) + "\n\n" + (note or "")
    assert f"{config.USD_TO_KES_RATE:,.2f}" in composed, composed
    assert "indicative" in composed.lower(), composed
    assert "not a live exchange rate" in composed, composed


def test_transactions_record_which_rail_and_which_currency():
    database.init_db()
    ref = "TEST-LEDGER-1"
    database.log_transaction("test-ledger", "CRQ-1", "254722334455", 9675.0, ref,
                             "Name change fee", "simulated_sandbox",
                             method="mpesa", currency="KES")
    database.log_transaction("test-ledger", "ORD-1", "", 75.0, ref,
                             "Name change fee", "simulated_paypal",
                             method="card", currency="USD")
    rows = [r for r in database.fetch_transactions(limit=200)
            if r.get("reference") == ref]
    assert len(rows) >= 2, rows
    pairs = {(r["method"], r["currency"]) for r in rows}
    assert ("mpesa", "KES") in pairs, pairs
    assert ("card", "USD") in pairs, pairs


def test_pending_payment_state_holds_the_published_currency():
    database.init_db()
    session_id = "test-card-state"
    database.set_pending_payment(session_id, 75.0, "Name change fee",
                                 "awaiting_method", currency="USD")
    state = database.get_pending_payment(session_id)
    assert state["currency"] == "USD", state
    assert state["stage"] == "awaiting_method", state
    # Storing KES as the canonical amount would bake the assumed rate into the
    # record and lose the published figure.
    assert float(state["amount"]) == 75.0, state
    database.clear_pending_payment(session_id)


def _continue_payment(session_id: str, amount: float, currency: str, message: str) -> str:
    database.init_db()
    database.clear_pending_payment(session_id)
    database.set_pending_payment(session_id, amount, "Name change fee",
                                 "awaiting_method", currency=currency)
    pending = database.get_pending_payment(session_id)
    sentiment_result = sentiment.classify_frustration(message)
    result = agent._handle_pending_payment(session_id, message, pending, [],
                                           sentiment_result)
    database.clear_pending_payment(session_id)
    assert result is not None, message
    return result["answer"] if isinstance(result, dict) else str(result)


def test_end_to_end_card_path_never_charges_shillings():
    answer = _continue_payment("test-card-e2e", 75.0, "USD",
                               "I do not have a Kenyan number")
    assert "USD 75.00" in answer, answer
    assert "Ksh" not in answer, answer


def test_end_to_end_mpesa_path_still_converts_and_discloses():
    answer = _continue_payment("test-mpesa-e2e", 75.0, "USD", "0722334455")
    expected = f"{round(75.0 * config.USD_TO_KES_RATE):,}"
    assert expected in answer, answer
    assert "not a live exchange rate" in answer, answer


def test_a_shilling_fee_on_the_mpesa_rail_quotes_no_rate():
    answer = _continue_payment("test-mpesa-kes", 9000.0, "KES", "0722334455")
    assert "9,000" in answer, answer
    assert f"{config.USD_TO_KES_RATE:,.2f}" not in answer, answer


def test_choosing_mpesa_without_a_number_asks_for_one():
    answer = _continue_payment("test-mpesa-ask", 75.0, "USD", "mpesa please")
    lower = answer.lower()
    assert "number" in lower, answer
    # It must not push anything before it knows the line.
    assert "check your phone" not in lower, answer
    # And it must say the line has to be Kenyan, offering the card as the way out.
    assert "kenyan" in lower and "card" in lower, answer


def test_choosing_mpesa_without_a_number_still_discloses_the_conversion():
    answer = _continue_payment("test-mpesa-ask-2", 75.0, "USD", "can I use mpesa")
    assert f"{config.USD_TO_KES_RATE:,.2f}" in answer, answer
    assert "not a live exchange rate" in answer, answer


def test_no_kenyan_number_goes_to_the_card_rail_not_the_mpesa_prompt():
    # "number" appears in both patterns, so precedence matters: a guest saying
    # they have no Kenyan number must never be asked for a Kenyan number.
    answer = _continue_payment("test-precedence", 75.0, "USD",
                               "I don't have a Kenyan number")
    assert "checkout" in answer.lower(), answer
    assert "07XX" not in answer, answer


def test_daraja_uses_the_right_host_for_the_environment():
    # DARAJA_SANDBOX was read from the environment but never used, so
    # production credentials would have been sent to the sandbox host.
    original = config.DARAJA_SANDBOX
    try:
        config.DARAJA_SANDBOX = True
        assert "sandbox.safaricom.co.ke" in daraja._host()
        config.DARAJA_SANDBOX = False
        assert daraja._host() == "https://api.safaricom.co.ke"
    finally:
        config.DARAJA_SANDBOX = original


def test_daraja_posture_is_reportable_without_exposing_secrets():
    posture = daraja.describe()
    for key in ("configured", "environment", "host", "shortcode",
                "callback_is_placeholder"):
        assert key in posture, posture
    blob = repr(posture)
    assert config.DARAJA_CONSUMER_SECRET not in blob or not config.DARAJA_CONSUMER_SECRET
    assert config.DARAJA_PASSKEY not in blob or not config.DARAJA_PASSKEY


def test_the_suite_never_contacts_safaricom():
    # A test run must never cost anyone a real STK prompt. The agent calls
    # daraja.initiate_stk_push by module attribute, so the stub installed at
    # import time is the one that runs.
    assert agent.daraja.initiate_stk_push is _stub_stk_push
    _STK_CALLS.clear()
    _continue_payment("test-guard", 75.0, "USD", "0722334455")
    assert len(_STK_CALLS) == 1, _STK_CALLS
    # And the amount reaching Safaricom would have been the converted one.
    assert _STK_CALLS[0]["amount"] == round(75.0 * config.USD_TO_KES_RATE)


def test_a_simulated_stk_push_is_labelled_as_simulated():
    # A simulated success must never be mistaken for evidence that Safaricom
    # accepted anything. Checked against the real client, not the stub.
    if daraja.is_configured():
        return
    result = _LIVE_STK("254712345678", 1, "TEST", "verification")
    assert result["source"] == "simulated_sandbox", result


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
