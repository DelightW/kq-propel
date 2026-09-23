"""
Safaricom Daraja M-Pesa payment gateway client (STK Push).

Uses the live Daraja sandbox/production API when credentials are configured;
otherwise simulates the STK push flow so the payment tool can be exercised
end-to-end in a demo environment without real M-Pesa credentials.
"""
import base64
import random
import string
import uuid
from datetime import datetime
from typing import Dict

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


def _get_access_token() -> str:
    resp = requests.get(
        _auth_url(),
        auth=(config.DARAJA_CONSUMER_KEY, config.DARAJA_CONSUMER_SECRET),
        timeout=8,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _password_and_timestamp():
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    raw = f"{config.DARAJA_SHORTCODE}{config.DARAJA_PASSKEY}{timestamp}"
    password = base64.b64encode(raw.encode()).decode()
    return password, timestamp


def initiate_stk_push(phone_number: str, amount: float, reference: str, description: str) -> Dict:
    if is_configured():
        environment = "sandbox" if config.DARAJA_SANDBOX else "production"
        try:
            token = _get_access_token()
            password, timestamp = _password_and_timestamp()
            # Safaricom rejects zero and fractional amounts; the STK prompt is
            # denominated in whole shillings.
            whole_shillings = max(1, int(round(amount)))
            payload = {
                "BusinessShortCode": config.DARAJA_SHORTCODE,
                "Password": password,
                "Timestamp": timestamp,
                "TransactionType": "CustomerPayBillOnline",
                "Amount": whole_shillings,
                "PartyA": phone_number,
                "PartyB": config.DARAJA_SHORTCODE,
                "PhoneNumber": phone_number,
                "CallBackURL": config.DARAJA_CALLBACK_URL,
                "AccountReference": reference,
                "TransactionDesc": description,
            }
            resp = requests.post(
                _stk_url(),
                json=payload,
                headers={"Authorization": "Bearer " + token},
                timeout=8,
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                "source": f"daraja_{environment}",
                "success": True,
                "checkout_request_id": data.get("CheckoutRequestID"),
                "merchant_request_id": data.get("MerchantRequestID"),
                "amount": whole_shillings,
                "phone_number": phone_number,
                "reference": reference,
                "message": data.get("CustomerMessage", "STK push sent."),
            }
        except Exception as exc:
            detail = str(exc)
            # Safaricom puts the reason in the body; "400 Client Error" on its
            # own is not actionable when a push fails during a demonstration.
            response = getattr(exc, "response", None)
            if response is not None:
                try:
                    detail = f"{detail} - {response.text[:300]}"
                except Exception:
                    pass
            return {"source": f"daraja_{environment}", "success": False,
                    "amount": amount, "phone_number": phone_number,
                    "reference": reference, "error": detail}

    # Simulated sandbox response
    checkout_id = "ws_CO_" + "".join(random.choices(string.digits, k=12))
    return {
        "source": "simulated_sandbox",
        "success": True,
        "checkout_request_id": checkout_id,
        "merchant_request_id": str(uuid.uuid4()),
        "amount": amount,
        "phone_number": phone_number,
        "reference": reference,
        "message": (
            f"An M-Pesa STK push for Ksh {amount:,.0f} has been sent to {phone_number}. "
            f"Please enter your M-Pesa PIN to complete payment for: {description}."
        ),
    }
