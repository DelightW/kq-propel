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

_DARAJA_AUTH_URL = "https://sandbox.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"
_DARAJA_STK_URL = "https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest"


def _get_access_token() -> str:
    resp = requests.get(
        _DARAJA_AUTH_URL,
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
    if config.DARAJA_CONSUMER_KEY and config.DARAJA_CONSUMER_SECRET and config.DARAJA_PASSKEY:
        try:
            token = _get_access_token()
            password, timestamp = _password_and_timestamp()
            payload = {
                "BusinessShortCode": config.DARAJA_SHORTCODE,
                "Password": password,
                "Timestamp": timestamp,
                "TransactionType": "CustomerPayBillOnline",
                "Amount": int(amount),
                "PartyA": phone_number,
                "PartyB": config.DARAJA_SHORTCODE,
                "PhoneNumber": phone_number,
                "CallBackURL": "https://example.com/daraja/callback",
                "AccountReference": reference,
                "TransactionDesc": description,
            }
            resp = requests.post(
                _DARAJA_STK_URL,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=8,
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                "source": "daraja_sandbox",
                "success": True,
                "checkout_request_id": data.get("CheckoutRequestID"),
                "merchant_request_id": data.get("MerchantRequestID"),
                "message": data.get("CustomerMessage", "STK push sent."),
            }
        except Exception as exc:
            return {"source": "daraja_sandbox", "success": False, "error": str(exc)}

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
