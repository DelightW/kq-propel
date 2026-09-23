"""
PayPal Orders API v2 client - the card payment rail.

Why this exists alongside Daraja: an M-Pesa STK push can only be sent to a
Safaricom subscriber, so the in-chat payment feature was unusable for any guest
without a Kenyan number - which is most of the network Kenya Airways flies.
Kenya Airways publishes that it accepts local and international credit and
debit cards, so this rail is grounded in the corpus rather than invented.

It also removes an integrity problem instead of managing one. A card can be
charged in the currency the fee is published in, so the dollar fees Kenya
Airways lists need no exchange rate at all. The declared `USD_TO_KES_RATE`
applies only to M-Pesa, because Daraja settles exclusively in shillings.

Like the Daraja client, this runs against the real API when credentials are
configured and simulates the flow otherwise, so the end-to-end conversation can
be demonstrated without credentials. The two paths are distinguishable in the
`source` field - a simulated approval link is clearly marked and is not a real
checkout URL.
"""
import uuid
from typing import Dict, Optional

import requests

from app import config

_LIVE_BASE = "https://api-m.paypal.com"
_SANDBOX_BASE = "https://api-m.sandbox.paypal.com"

# Set when a live call fails, so a fallback to simulation is never silent.
last_live_error: Optional[str] = None


def _base_url() -> str:
    return _SANDBOX_BASE if config.PAYPAL_SANDBOX else _LIVE_BASE


def is_configured() -> bool:
    return bool(config.PAYPAL_CLIENT_ID and config.PAYPAL_CLIENT_SECRET)


def _access_token() -> str:
    resp = requests.post(
        f"{_base_url()}/v1/oauth2/token",
        auth=(config.PAYPAL_CLIENT_ID, config.PAYPAL_CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
        headers={"Accept": "application/json"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _approval_link(order: Dict) -> Optional[str]:
    for link in order.get("links", []):
        if link.get("rel") in {"approve", "payer-action"}:
            return link.get("href")
    return None


def _simulated_order(amount: float, currency: str, reference: str,
                     description: str, source: str) -> Dict:
    order_id = uuid.uuid4().hex[:17].upper()
    return {
        "source": source,
        "success": True,
        "order_id": order_id,
        "status": "CREATED",
        # Deliberately not a paypal.com URL. A simulated link that looked real
        # would be indistinguishable from a live one in a screenshot.
        "approval_url": f"https://example.com/simulated-paypal-checkout/{order_id}",
        "amount": amount,
        "currency": currency,
        "reference": reference,
        "message": (
            f"A secure card checkout for {currency} {amount:,.2f} has been created for: "
            f"{description}. Open the link to enter your card details - Visa, Mastercard "
            f"and American Express are accepted, and you do not need a PayPal account."
        ),
    }


def create_card_order(amount: float, currency: str, reference: str,
                      description: str) -> Dict:
    """Creates a PayPal order and returns the link the guest approves it at.

    The order is created for the amount in its own currency; nothing here
    converts anything.
    """
    global last_live_error

    if is_configured():
        try:
            token = _access_token()
            resp = requests.post(
                f"{_base_url()}/v2/checkout/orders",
                json={
                    "intent": "CAPTURE",
                    "purchase_units": [{
                        "reference_id": reference,
                        "description": description[:127],
                        "amount": {
                            "currency_code": currency,
                            "value": f"{amount:.2f}",
                        },
                    }],
                    "payment_source": {
                        "paypal": {
                            "experience_context": {
                                # Card-first: the guest should not need a PayPal
                                # account to pay an airline fee.
                                "shipping_preference": "NO_SHIPPING",
                                "user_action": "PAY_NOW",
                                "return_url": config.PAYPAL_RETURN_URL,
                                "cancel_url": config.PAYPAL_CANCEL_URL,
                            }
                        }
                    },
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    # Makes the create-order call safe to retry.
                    "PayPal-Request-Id": reference,
                },
                timeout=12,
            )
            resp.raise_for_status()
            order = resp.json()
            return {
                "source": "paypal_sandbox" if config.PAYPAL_SANDBOX else "paypal_live",
                "success": True,
                "order_id": order.get("id"),
                "status": order.get("status"),
                "approval_url": _approval_link(order),
                "amount": amount,
                "currency": currency,
                "reference": reference,
                "message": (
                    f"A secure card checkout for {currency} {amount:,.2f} has been created "
                    f"for: {description}."
                ),
            }
        except Exception as exc:
            # Recorded, not swallowed: a configured-but-failing gateway must be
            # distinguishable from one that was never configured.
            last_live_error = str(exc)
            return _simulated_order(amount, currency, reference, description,
                                    source="simulated_after_live_error")

    return _simulated_order(amount, currency, reference, description,
                            source="simulated_sandbox")


def describe() -> Dict[str, object]:
    """Credential posture, for the dashboard and the credential checker."""
    info: Dict[str, object] = {
        "configured": is_configured(),
        "environment": "sandbox" if config.PAYPAL_SANDBOX else "live",
        "mode": "live API" if is_configured() else "simulated checkout",
    }
    if last_live_error:
        info["last_live_error"] = last_live_error
    return info
