"""
Safaricom Daraja M-Pesa payment gateway client (STK Push).

Uses the live Daraja sandbox/production API when credentials are configured;
otherwise simulates the STK push flow so the payment tool can be exercised
end-to-end in a demo environment without real M-Pesa credentials.
"""
import base64
import random
import string
import threading
import time
import uuid
from datetime import datetime
from typing import Dict, Optional

import requests

from app import config

_SANDBOX_HOST = "https://sandbox.safaricom.co.ke"
_LIVE_HOST = "https://api.safaricom.co.ke"


def _host() -> str:
    """Sandbox and production are different hosts, not different credentials.

    DARAJA_SANDBOX was previously read from the environment but never used, so
    production credentials would have been sent to the sandbox host.
    """
    return _SANDBOX_HOST if config.DARAJA_SANDBOX else _LIVE_HOST


def _auth_url() -> str:
    return f"{_host()}/oauth/v1/generate?grant_type=client_credentials"


def _stk_url() -> str:
    return f"{_host()}/mpesa/stkpush/v1/processrequest"


def is_configured() -> bool:
    return bool(config.DARAJA_CONSUMER_KEY and config.DARAJA_CONSUMER_SECRET
                and config.DARAJA_PASSKEY)


def describe() -> Dict[str, object]:
    """Reports the posture without revealing the credentials."""
    return {
        "configured": is_configured(),
        "environment": "sandbox" if config.DARAJA_SANDBOX else "production",
        "host": _host(),
        "shortcode": config.DARAJA_SHORTCODE,
        "callback_url": config.DARAJA_CALLBACK_URL,
        "callback_is_placeholder": "example.com" in config.DARAJA_CALLBACK_URL,
    }


# A Daraja token is valid for an hour. Requesting a new one for every push and
# every status check made Safaricom's firewall answer 403 after a handful of
# calls (seen on 5 and 10 November), which failed real payments.
_token_cache = {"value": None, "expires": 0.0, "host": None}
_token_lock = threading.Lock()


def _get_access_token() -> str:
    with _token_lock:
        now = time.monotonic()
        if (_token_cache["value"] and _token_cache["host"] == _host()
                and now < _token_cache["expires"]):
            return _token_cache["value"]
        resp = requests.get(
            _auth_url(),
            auth=(config.DARAJA_CONSUMER_KEY, config.DARAJA_CONSUMER_SECRET),
            timeout=8,
        )
        resp.raise_for_status()
        body = resp.json()
        try:
            lifetime = int(body.get("expires_in", 3599))
        except (TypeError, ValueError):
            lifetime = 3599
        # Renew a minute early so a token never expires mid-request.
        _token_cache.update(value=body["access_token"], host=_host(),
                            expires=now + max(lifetime - 60, 30))
        return _token_cache["value"]


def _forget_token() -> None:
    """Called when Safaricom rejects a token, so the next call fetches a new one."""
    with _token_lock:
        _token_cache.update(value=None, expires=0.0)


def _password_and_timestamp():
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    raw = f"{config.DARAJA_SHORTCODE}{config.DARAJA_PASSKEY}{timestamp}"
    password = base64.b64encode(raw.encode()).decode()
    return password, timestamp


def _error_detail(exc: Exception) -> str:
    detail = str(exc)
    # Safaricom puts the reason in the body; "400 Client Error" on its own is
    # not actionable.
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            detail = f"{detail} - {response.text[:300]}"
        except Exception:
            pass
    return detail


def initiate_stk_push(phone_number: str, amount: float, reference: str, description: str) -> Dict:
    """Sends an STK Push and reports whether Safaricom *accepted the request*.

    Acceptance is not payment. It means the prompt is on its way to the handset;
    the payment's outcome arrives later through the callback or the status
    query (see payments.py). Acceptance is judged on Safaricom's own
    ResponseCode, not on the HTTP status: a 200 response can still carry a
    rejection, and treating it as success showed a passenger a confirmation
    for a prompt that was never sent.
    """
    whole_shillings = max(1, int(round(amount)))
    fee_shillings = whole_shillings
    sandbox_blocked = False
    if is_configured() and config.DARAJA_SANDBOX:
        # The sandbox is not play money. A prompt to a real Safaricom number
        # takes real money when the PIN is entered (it goes to Safaricom's test
        # Paybill 174379), and a prompt to someone else's number is unsolicited.
        # So sandbox prompts go only to numbers the operator has listed, and
        # always for Ksh 1; every other number gets the labelled demonstration.
        if normalize_phone(phone_number) in sandbox_test_numbers():
            whole_shillings = 1
        else:
            sandbox_blocked = True
    if is_configured() and not sandbox_blocked:
        environment = "sandbox" if config.DARAJA_SANDBOX else "production"
        try:
            token = _get_access_token()
            password, timestamp = _password_and_timestamp()
            payload = {
                "BusinessShortCode": config.DARAJA_SHORTCODE,
                "Password": password,
                "Timestamp": timestamp,
                "TransactionType": config.DARAJA_TRANSACTION_TYPE,
                # Safaricom rejects zero and fractional amounts; the STK prompt
                # is denominated in whole shillings.
                "Amount": whole_shillings,
                "PartyA": phone_number,
                "PartyB": config.DARAJA_PARTY_B,
                "PhoneNumber": phone_number,
                "CallBackURL": config.DARAJA_CALLBACK_URL,
                "AccountReference": reference[:12],
                "TransactionDesc": description[:13],
            }
            resp = requests.post(_stk_url(), json=payload,
                                 headers={"Authorization": "Bearer " + token}, timeout=8)
            if resp.status_code == 401:
                _forget_token()
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            return {"source": f"daraja_{environment}", "success": False,
                    "amount": whole_shillings, "phone_number": phone_number,
                    "reference": reference, "error": _error_detail(exc)}

        accepted = str(data.get("ResponseCode", "")).strip() == "0"
        checkout_id = data.get("CheckoutRequestID")
        if not accepted or not checkout_id:
            return {"source": f"daraja_{environment}", "success": False,
                    "amount": whole_shillings, "phone_number": phone_number,
                    "reference": reference,
                    "error": (data.get("ResponseDescription") or data.get("errorMessage")
                              or "Safaricom did not accept the payment request.")}
        return {
            "source": f"daraja_{environment}",
            "success": True,
            "checkout_request_id": checkout_id,
            "merchant_request_id": data.get("MerchantRequestID"),
            "amount": whole_shillings,
            "phone_number": phone_number,
            "reference": reference,
            "message": data.get("CustomerMessage", "STK push sent."),
            # Set for a sandbox test prompt: what the fee was, as against the
            # Ksh 1 actually requested.
            "sandbox_test_fee": fee_shillings if config.DARAJA_SANDBOX else None,
        }

    # Demonstration only: no request leaves this machine.
    checkout_id = "ws_CO_SIM_" + "".join(random.choices(string.digits, k=12))
    return {
        "source": "simulated",
        "success": True,
        "checkout_request_id": checkout_id,
        "merchant_request_id": str(uuid.uuid4()),
        "amount": whole_shillings,
        "phone_number": phone_number,
        "reference": reference,
        "message": (
            f"Demonstration only: an M-Pesa prompt for Ksh {whole_shillings:,} would now be "
            f"sent to {phone_number}. No real payment request was made."
        ),
        "sandbox_blocked": sandbox_blocked,
    }


def normalize_phone(phone: str) -> str:
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if digits.startswith("0") and len(digits) == 10:
        digits = "254" + digits[1:]
    elif len(digits) == 9 and digits[0] in "17":
        digits = "254" + digits
    return digits


def sandbox_test_numbers() -> set:
    return {normalize_phone(n) for n in config.DARAJA_SANDBOX_TEST_NUMBERS.split(",") if n.strip()}


# Daraja ResultCode -> lifecycle status. Codes not listed are failures.
# 0 paid; 1032 cancelled by the customer; 1037 no response from the handset
# (timed out); 1 insufficient balance; 2001 wrong PIN.
_RESULT_STATUS = {"0": "paid", "1032": "cancelled", "1037": "expired"}


def result_status(result_code) -> str:
    return _RESULT_STATUS.get(str(result_code).strip(), "failed")


def query_stk_status(checkout_request_id: str) -> Dict:
    """Asks Safaricom for the outcome of an STK Push.

    Used to confirm a success callback before trusting it, and to resolve
    payments whose callback never arrived. Returns {"status": None} when the
    outcome is not yet known (the customer has not responded) or the query
    could not be made.
    """
    if not is_configured():
        return {"status": None, "detail": "Daraja is not configured."}
    # The chat polls a pending payment every few seconds. Asking Safaricom each
    # time trips its firewall, so one payment is queried at most once per
    # STATUS_QUERY_MIN_INTERVAL seconds; in between the answer is "not yet known".
    now = time.monotonic()
    last = _last_query.get(checkout_request_id)
    if last is not None and now - last < STATUS_QUERY_MIN_INTERVAL:
        return {"status": None, "detail": "checked recently"}
    _last_query[checkout_request_id] = now
    try:
        token = _get_access_token()
        password, timestamp = _password_and_timestamp()
        resp = requests.post(
            f"{_host()}/mpesa/stkpushquery/v1/query",
            json={"BusinessShortCode": config.DARAJA_SHORTCODE, "Password": password,
                  "Timestamp": timestamp, "CheckoutRequestID": checkout_request_id},
            headers={"Authorization": "Bearer " + token}, timeout=8)
        if resp.status_code == 401:
            _forget_token()
        data = resp.json()
    except Exception as exc:
        # Includes the firewall's HTML page, which is not JSON: unknown, not failed.
        return {"status": None, "detail": _error_detail(exc)[:300]}
    return interpret_query(data)


STATUS_QUERY_MIN_INTERVAL = 15.0
_last_query: Dict[str, float] = {}
# Answers that mean "the passenger has not finished yet", not an outcome.
# Safaricom sends 4999 "The transaction is still under processing" with a
# ResultCode, which the code used to read as a failure - a payment would have
# been marked failed for good while the passenger was entering their PIN.
_STILL_PROCESSING_CODES = {"4999"}
_STILL_PROCESSING_TEXT = ("under processing", "being processed", "still processing")


def interpret_query(data: Dict) -> Dict:
    """Outcome of an STK status query: paid, cancelled, expired, failed, or
    None while the passenger has not finished."""
    if not isinstance(data, dict):
        return {"status": None, "detail": "unexpected response"}
    desc = str(data.get("ResultDesc") or data.get("errorMessage")
               or data.get("ResponseDescription") or "")
    code = data.get("ResultCode")
    if code is None or str(code).strip() in _STILL_PROCESSING_CODES \
            or any(t in desc.lower() for t in _STILL_PROCESSING_TEXT):
        return {"status": None, "detail": desc or "pending"}
    return {"status": result_status(code), "detail": desc}


def parse_callback(body: Dict) -> Optional[Dict]:
    """Extracts the outcome from an STK callback body, or None if malformed."""
    try:
        cb = body["Body"]["stkCallback"]
        checkout_id = cb["CheckoutRequestID"]
        code = cb["ResultCode"]
    except (KeyError, TypeError):
        return None
    items = {}
    for item in ((cb.get("CallbackMetadata") or {}).get("Item") or []):
        if isinstance(item, dict) and "Name" in item:
            items[item["Name"]] = item.get("Value")
    return {
        "checkout_request_id": checkout_id,
        "status": result_status(code),
        "result_code": str(code),
        "detail": cb.get("ResultDesc") or "",
        "amount": items.get("Amount"),
        "receipt": items.get("MpesaReceiptNumber"),
        "phone_number": str(items.get("PhoneNumber") or "") or None,
    }
