"""Daraja client safeguards, found by a real sandbox payment on 10 November.

  * The sandbox takes real money, so sandbox prompts go only to listed test
    numbers, always for Ksh 1; every other number is simulated and nothing is
    sent to Safaricom.
  * The access token is reused for its lifetime; fetching one per call made
    Safaricom's firewall answer 403.
  * "Still under processing" (ResultCode 4999) and error replies mean the
    outcome is not known yet - never "failed".
  * One payment is queried at most once per STATUS_QUERY_MIN_INTERVAL seconds.

No request leaves this machine: HTTP calls are stubbed.
Run:  python tools/test_daraja_client.py
"""
import sys
import traceback
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app import config, daraja  # noqa: E402


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body if body is not None else {}

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not JSON")
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    @property
    def text(self):
        return str(self._body)


class Stub:
    """Records calls and plays back responses for requests.get / requests.post."""

    def __init__(self):
        self.gets, self.posts = [], []
        self.post_response = Resp(200, {"ResponseCode": "0", "CheckoutRequestID": "ws_CO_TEST",
                                        "MerchantRequestID": "m1", "CustomerMessage": "ok"})

    def get(self, url, **kw):
        self.gets.append(url)
        return Resp(200, {"access_token": f"tok{len(self.gets)}", "expires_in": "3599"})

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append((url, json, headers))
        return self.post_response


def setup(test_numbers=""):
    saved = (config.DARAJA_CONSUMER_KEY, config.DARAJA_CONSUMER_SECRET, config.DARAJA_PASSKEY,
             config.DARAJA_SANDBOX, config.DARAJA_SANDBOX_TEST_NUMBERS,
             daraja.requests.get, daraja.requests.post)
    stub = Stub()
    config.DARAJA_CONSUMER_KEY, config.DARAJA_CONSUMER_SECRET, config.DARAJA_PASSKEY = "k", "s", "p"
    config.DARAJA_SANDBOX, config.DARAJA_SANDBOX_TEST_NUMBERS = True, test_numbers
    daraja.requests.get, daraja.requests.post = stub.get, stub.post
    daraja._forget_token()
    daraja._last_query.clear()
    return stub, saved


def restore(saved):
    (config.DARAJA_CONSUMER_KEY, config.DARAJA_CONSUMER_SECRET, config.DARAJA_PASSKEY,
     config.DARAJA_SANDBOX, config.DARAJA_SANDBOX_TEST_NUMBERS,
     daraja.requests.get, daraja.requests.post) = saved
    daraja._forget_token()


def test_sandbox_never_prompts_an_unlisted_number():
    stub, saved = setup(test_numbers="")
    try:
        r = daraja.initiate_stk_push("254700000001", 9675, "KQ-1", "fee")
        assert r["source"] == "simulated" and r["sandbox_blocked"], r
        assert stub.posts == [] and stub.gets == [], "nothing sent to Safaricom"
    finally:
        restore(saved)


def test_sandbox_prompts_a_listed_number_for_one_shilling():
    stub, saved = setup(test_numbers="0704 000 111, 0799000222")
    try:
        r = daraja.initiate_stk_push("254704000111", 9675, "KQ-2", "fee")
        assert r["success"] and r["source"] == "daraja_sandbox" and r["amount"] == 1, r
        assert r["sandbox_test_fee"] == 9675
        assert stub.posts[-1][1]["Amount"] == 1, "Ksh 1 requested, never the fee"
    finally:
        restore(saved)


def test_production_sends_the_real_fee_to_any_number():
    stub, saved = setup()
    config.DARAJA_SANDBOX = False
    try:
        r = daraja.initiate_stk_push("254700000001", 9675, "KQ-3", "fee")
        assert r["success"] and r["amount"] == 9675 and r["sandbox_test_fee"] is None, r
        assert stub.posts[-1][0].startswith("https://api.safaricom.co.ke")
    finally:
        restore(saved)


def test_access_token_is_reused():
    stub, saved = setup(test_numbers="0704000111")
    try:
        daraja.initiate_stk_push("0704000111", 1, "KQ-4", "fee")
        daraja.initiate_stk_push("0704000111", 1, "KQ-5", "fee")
        daraja.query_stk_status("ws_CO_A")
        assert len(stub.gets) == 1, f"one token for three calls, got {len(stub.gets)}"
        stub.post_response = Resp(401, {})
        daraja.query_stk_status("ws_CO_B")
        daraja.query_stk_status("ws_CO_C")
        assert len(stub.gets) == 2, "a rejected token is replaced"
    finally:
        restore(saved)


def test_still_processing_is_not_a_failure():
    cases = [
        ({"ResultCode": "4999", "ResultDesc": "The transaction is still under processing"}, None),
        ({"errorCode": "500.001.1001", "errorMessage": "The transaction is being processed"}, None),
        ({"requestId": "ws_CO_X", "errorCode": "500.001.1001", "errorMessage": ""}, None),
        ({"ResultCode": "0", "ResultDesc": "The service request is processed successfully."}, "paid"),
        ({"ResultCode": "1032", "ResultDesc": "Request cancelled by user"}, "cancelled"),
        ({"ResultCode": "1037", "ResultDesc": "DS timeout user cannot be reached"}, "expired"),
        ({"ResultCode": "2001", "ResultDesc": "The initiator information is invalid."}, "failed"),
    ]
    for body, expected in cases:
        assert daraja.interpret_query(body)["status"] == expected, body


def test_firewall_page_is_not_a_failure_and_queries_are_spaced():
    stub, saved = setup(test_numbers="0704000111")
    try:
        stub.post_response = Resp(403, "<html>Incapsula</html>")
        assert daraja.query_stk_status("ws_CO_FW")["status"] is None
        before = len(stub.posts)
        assert daraja.query_stk_status("ws_CO_FW")["detail"] == "checked recently"
        assert len(stub.posts) == before, "second query within the interval is not sent"
    finally:
        restore(saved)


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
