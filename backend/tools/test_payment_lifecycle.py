"""Payment lifecycle tests: ledger, callbacks, idempotency, expiry and totals.

Runs against a temporary database and fake providers. It never contacts
Safaricom or PayPal and never touches backend/data/kq_propel.db.

Run:  python tools/test_payment_lifecycle.py
"""
import sys
import tempfile
import traceback
from datetime import datetime, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402

config.APP_DB_PATH = Path(tempfile.mkdtemp()) / "lifecycle.db"
config.DARAJA_CONSUMER_KEY = config.DARAJA_CONSUMER_SECRET = config.DARAJA_PASSKEY = ""
config.PAYPAL_CLIENT_ID = config.PAYPAL_CLIENT_SECRET = ""
config.DARAJA_CALLBACK_TOKEN = "test-callback-secret"

from app import agent, daraja, database, payments, paypal  # noqa: E402

database.init_db()

# ------------------------------------------------------------------ fakes

STK_CALLS = []


def fake_stk(phone_number, amount, reference, description):
    STK_CALLS.append({"phone": phone_number, "amount": amount, "reference": reference})
    return {"source": "daraja_sandbox", "success": True, "amount": int(round(amount)),
            "checkout_request_id": f"ws_CO_{reference}", "phone_number": phone_number,
            "reference": reference, "message": "accepted"}


QUERY_ANSWER = {"status": "paid", "detail": "The service request is processed successfully."}


def fake_query(checkout_id):
    return dict(QUERY_ANSWER)


daraja.initiate_stk_push = fake_stk
daraja.query_stk_status = fake_query
daraja.is_configured = lambda: True


def callback(checkout_id, code=0, amount=None, receipt="RCP123"):
    items = [{"Name": "Amount", "Value": amount}, {"Name": "MpesaReceiptNumber", "Value": receipt}]
    return {"Body": {"stkCallback": {"CheckoutRequestID": checkout_id, "ResultCode": code,
                                     "ResultDesc": "test", "CallbackMetadata": {"Item": items}}}}


def pending_attempt(session="s-1", kes=9675, quote=True):
    quote_id = payments.new_quote(session, kes, "KES", "Name change fee") if quote else None
    result = agent._execute_payment(session, "254712345678", kes, "Name change fee", [],
                                    quote_id=quote_id)
    return result, payments.for_passenger(session, result["reference"])


# ------------------------------------------------------------------ tests

def test_amounts_are_stored_exactly_in_minor_units():
    assert payments.to_minor(9675.0) == 967500
    assert payments.to_minor("0.1") + payments.to_minor("0.2") == payments.to_minor("0.3")
    assert payments.to_minor(75) == 7500 and payments.to_minor(19.995) == 2000


def test_a_sent_prompt_is_pending_not_paid():
    _, row = pending_attempt("s-pending")
    assert row["status"] == payments.PENDING, row
    assert row["amount_minor"] == 967500


def test_success_callback_with_matching_amount_settles_once():
    result, row = pending_attempt("s-paid")
    changed, row = payments.apply_mpesa_outcome(daraja.parse_callback(
        callback(row["checkout_request_id"], 0, 9675)))
    assert changed and row["status"] == payments.PAID and row["provider_receipt"] == "RCP123"
    # A repeated callback and a late failure both leave the payment paid.
    again, _ = payments.apply_mpesa_outcome(daraja.parse_callback(
        callback(row["checkout_request_id"], 0, 9675)))
    late, row = payments.apply_mpesa_outcome(daraja.parse_callback(
        callback(row["checkout_request_id"], 1, 9675)))
    assert not again and not late and row["status"] == payments.PAID


def test_late_callback_adds_the_receipt_to_a_query_settled_payment():
    # Seen in the real sandbox test on 10 November: the status query settled
    # the payment first, and the callback carrying the receipt was ignored.
    _, row = pending_attempt("s-receipt")
    payments.reconcile(row)
    row = payments.for_passenger("s-receipt", row["reference"])
    assert row["status"] == payments.PAID and not row["provider_receipt"], row
    _, row = payments.apply_mpesa_outcome(daraja.parse_callback(
        callback(row["checkout_request_id"], 0, 9675, receipt="SKA1B2C3D4")))
    assert row["status"] == payments.PAID and row["provider_receipt"] == "SKA1B2C3D4", row
    # A callback for a different amount, or a failure, never touches it.
    _, row = payments.apply_mpesa_outcome(daraja.parse_callback(
        callback(row["checkout_request_id"], 1, 9675, receipt="OTHER")))
    assert row["provider_receipt"] == "SKA1B2C3D4"


def test_wrong_amount_is_flagged_for_review_not_paid():
    _, row = pending_attempt("s-amount")
    _, row = payments.apply_mpesa_outcome(daraja.parse_callback(
        callback(row["checkout_request_id"], 0, 1)))
    assert row["status"] == payments.NEEDS_REVIEW, row


def test_unconfirmed_success_callback_does_not_settle():
    _, row = pending_attempt("s-forged")
    QUERY_ANSWER["status"] = None          # Safaricom does not confirm it
    try:
        changed, row = payments.apply_mpesa_outcome(daraja.parse_callback(
            callback(row["checkout_request_id"], 0, 9675)))
    finally:
        QUERY_ANSWER["status"] = "paid"
    assert not changed and row["status"] == payments.PENDING


def test_cancel_and_timeout_codes_map_to_their_states():
    assert daraja.result_status(1032) == payments.CANCELLED
    assert daraja.result_status("1037") == payments.EXPIRED
    assert daraja.result_status(2001) == payments.FAILED
    _, row = pending_attempt("s-cancel")
    _, row = payments.apply_mpesa_outcome(daraja.parse_callback(
        callback(row["checkout_request_id"], 1032, 9675)))
    assert row["status"] == payments.CANCELLED


def test_same_quote_never_charges_twice():
    STK_CALLS.clear()
    quote = payments.new_quote("s-dup", 9675, "KES", "Name change fee")
    first = agent._execute_payment("s-dup", "254712345678", 9675, "fee", [], quote_id=quote)
    second = agent._execute_payment("s-dup", "254712345678", 9675, "fee", [], quote_id=quote)
    assert len(STK_CALLS) == 1, STK_CALLS
    assert second.get("duplicate") and second["reference"] == first["reference"]
    assert "already in progress" in agent._payment_sent_message(9675, "254712345678", second)


def test_a_failed_attempt_can_be_retried():
    STK_CALLS.clear()
    quote = payments.new_quote("s-retry", 9675, "KES", "fee")
    original = daraja.initiate_stk_push
    daraja.initiate_stk_push = lambda **k: {"source": "daraja_sandbox", "success": False,
                                            "error": "rejected", "amount": 9675}
    try:
        failed = agent._execute_payment("s-retry", "254712345678", 9675, "fee", [], quote_id=quote)
    finally:
        daraja.initiate_stk_push = original
    retry = agent._execute_payment("s-retry", "254712345678", 9675, "fee", [], quote_id=quote)
    assert failed["status"] == payments.FAILED and not retry.get("duplicate")
    assert len(STK_CALLS) == 1


def test_rejected_daraja_response_is_a_failure():
    import requests

    class Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"ResponseCode": "1", "ResponseDescription": "Rejected"}

    saved = (requests.post,)
    import importlib
    fresh = importlib.reload(daraja)
    fresh.is_configured = lambda: True
    fresh._get_access_token = lambda: "token"
    fresh.requests.post = lambda *a, **k: Resp()
    # Sandbox prompts only go to listed numbers; list this one so the stubbed
    # rejection is reached. Nothing leaves the machine.
    saved_numbers = config.DARAJA_SANDBOX_TEST_NUMBERS
    config.DARAJA_SANDBOX_TEST_NUMBERS = "254712345678"
    try:
        result = fresh.initiate_stk_push("254712345678", 100, "KQREF", "fee")
    finally:
        config.DARAJA_SANDBOX_TEST_NUMBERS = saved_numbers
        fresh.requests.post = saved[0]
        importlib.reload(daraja)
        daraja.initiate_stk_push = fake_stk
        daraja.query_stk_status = fake_query
        daraja.is_configured = lambda: True
    assert result["success"] is False and "Rejected" in result["error"], result
    assert "checkout_request_id" not in result


def test_simulated_payments_never_become_money():
    row, _ = payments.record_attempt(
        session_id="s-sim", quote_id=None, method="mpesa", currency="KES", amount=100,
        result={"source": "simulated", "success": True, "checkout_request_id": "SIM1"},
        reference="KQSIM00001")
    assert row["status"] == payments.SIMULATED
    changed, row = payments.transition(row["id"], payments.PAID)
    assert not changed and row["status"] == payments.SIMULATED


def test_expired_quote_is_not_settled():
    STK_CALLS.clear()
    payments.new_quote("s-expired", 9675, "KES", "fee")
    with database._connect() as conn:
        conn.execute("UPDATE payment_state SET expires_at = ? WHERE session_id = ?",
                     ((datetime.utcnow() - timedelta(minutes=1)).isoformat(), "s-expired"))
    pending = database.get_pending_payment("s-expired")
    turn = agent._handle_pending_payment("s-expired", "0712345678", pending, [],
                                         {"label": "calm", "frustration_score": 0.1})
    assert "expired" in turn["answer"] and not STK_CALLS


def test_stale_pending_payment_expires():
    _, row = pending_attempt("s-stale")
    with database._connect() as conn:
        conn.execute("UPDATE transactions SET created_at = ? WHERE id = ?",
                     ((datetime.utcnow() - timedelta(hours=1)).isoformat(), row["id"]))
    QUERY_ANSWER["status"] = None
    try:
        row = payments.reconcile(payments.for_passenger("s-stale", row["reference"]))
    finally:
        QUERY_ANSWER["status"] = "paid"
    assert row["status"] == payments.EXPIRED


def test_typed_amount_is_never_charged():
    STK_CALLS.clear()
    database.clear_pending_payment("s-typed")
    agent.run_agent_turn("s-typed", "I want to pay Ksh 1 now for my name correction on an "
                                    "international booking, my number is 0712345678")
    assert STK_CALLS, "expected a prompt for the policy fee"
    assert STK_CALLS[0]["amount"] != 1, STK_CALLS
    assert STK_CALLS[0]["amount"] == round(75 * config.USD_TO_KES_RATE), STK_CALLS


def test_settled_value_counts_paid_only():
    # The audit's fixture: a failed Ksh 5,000 and a pending Ksh 9,000 must
    # report nothing settled, not Ksh 14,000.
    before = payments.summary()["settled_by_currency"].get("KES", 0.0)
    payments.record_attempt(session_id="s-sum", quote_id=None, method="mpesa", currency="KES",
                            amount=5000, result={"source": "daraja_sandbox", "success": False},
                            reference="KQSUMFAIL1")
    pending_attempt("s-sum2", kes=9000, quote=False)
    summary = payments.summary()
    assert summary["settled_by_currency"].get("KES", 0.0) == before, summary
    assert summary["pending_by_currency"].get("KES", 0.0) >= 9000


def test_card_capture_must_match_amount_and_currency():
    row, _ = payments.record_attempt(
        session_id="s-card", quote_id=None, method="card", currency="USD", amount=75,
        result={"source": "paypal_sandbox", "success": True, "order_id": "ORDER1"},
        reference="KQCARD0001")
    payments.apply_card_outcome("ORDER1", {"status": "paid", "amount": "75.00", "currency": "KES"})
    assert payments.find_by_checkout("card", "ORDER1")["status"] == payments.NEEDS_REVIEW
    payments.record_attempt(
        session_id="s-card", quote_id=None, method="card", currency="USD", amount=75,
        result={"source": "paypal_sandbox", "success": True, "order_id": "ORDER2"},
        reference="KQCARD0002")
    payments.apply_card_outcome("ORDER2", {"status": "paid", "amount": "75.00", "currency": "USD",
                                           "receipt": "CAP2"})
    assert payments.find_by_checkout("card", "ORDER2")["status"] == payments.PAID


def test_configured_paypal_failure_is_not_a_fake_checkout():
    import requests
    saved = (config.PAYPAL_CLIENT_ID, config.PAYPAL_CLIENT_SECRET, requests.post)
    config.PAYPAL_CLIENT_ID, config.PAYPAL_CLIENT_SECRET = "id", "secret"

    def down(*a, **k):
        raise ConnectionError("gateway down")
    paypal.requests.post = down
    try:
        result = paypal.create_card_order(75, "USD", "KQREF", "fee")
    finally:
        config.PAYPAL_CLIENT_ID, config.PAYPAL_CLIENT_SECRET, paypal.requests.post = saved
    assert result["success"] is False and "approval_url" not in result, result


def test_http_endpoints():
    from fastapi.testclient import TestClient
    from app.main import app as fastapi_app
    client = TestClient(fastapi_app)

    # Callback secret is enforced.
    assert client.post("/api/payments/daraja/callback/wrong", json={}).status_code == 403
    _, row = pending_attempt("s-http")
    r = client.post(f"/api/payments/daraja/callback/{config.DARAJA_CALLBACK_TOKEN}",
                    json=callback(row["checkout_request_id"], 0, 9675))
    assert r.status_code == 200 and r.json()["ResultCode"] == 0
    assert payments.for_passenger("s-http", row["reference"])["status"] == payments.PAID

    # A passenger can read their own payment and nobody else's.
    owner = TestClient(fastapi_app)
    sid = owner.post("/api/chat/session").json()["session_id"]
    _, mine = pending_attempt(sid)
    QUERY_ANSWER["status"] = None          # Safaricom has no outcome yet
    try:
        status = owner.get(f"/api/chat/payments/{mine['reference']}").json()
    finally:
        QUERY_ANSWER["status"] = "paid"
    assert status["status"] == "pending" and status["final"] is False, status
    # The status endpoint asks the provider, so once Safaricom has an outcome
    # the passenger sees it even if the callback never arrived.
    status = owner.get(f"/api/chat/payments/{mine['reference']}").json()
    assert status["status"] == "paid" and status["final"] is True, status
    stranger = TestClient(fastapi_app)
    stranger.post("/api/chat/session")
    assert stranger.get(f"/api/chat/payments/{mine['reference']}").status_code == 404


def test_receipts_only_for_paid_payments_and_their_owner():
    from fastapi.testclient import TestClient
    from app.main import app as fastapi_app
    owner = TestClient(fastapi_app)
    sid = owner.post("/api/chat/session").json()["session_id"]
    _, waiting = pending_attempt(sid)
    QUERY_ANSWER["status"] = None
    try:
        assert owner.get(f"/api/chat/payments/{waiting['reference']}/receipt").status_code == 409
    finally:
        QUERY_ANSWER["status"] = "paid"
    payments.apply_mpesa_outcome(daraja.parse_callback(
        callback(waiting["checkout_request_id"], 0, 9675, receipt="SKR123TEST")))
    r = owner.get(f"/api/chat/payments/{waiting['reference']}/receipt")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf", r.status_code
    assert r.content.startswith(b"%PDF") and waiting["reference"] in r.headers["content-disposition"]
    stranger = TestClient(fastapi_app)
    stranger.post("/api/chat/session")
    assert stranger.get(f"/api/chat/payments/{waiting['reference']}/receipt").status_code == 404
    staff = TestClient(fastapi_app)
    assert staff.get(f"/api/admin/transactions/{waiting['reference']}/receipt").status_code == 401


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
