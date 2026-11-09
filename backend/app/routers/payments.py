"""
Provider-facing payment endpoints.

  POST /api/payments/daraja/callback/{token}   Safaricom STK Push result
  GET  /payment/return?token=<order id>        PayPal sends the guest back here
  GET  /payment/cancel?token=<order id>        ...or here if they cancel

None of these is trusted on its own word. Safaricom does not sign callbacks, so
the callback URL carries a secret token and a success is confirmed with
Safaricom's status query before it settles anything (payments.apply_mpesa_outcome).
Anyone can open the PayPal return URL, so the order is captured server-side and
PayPal's reported amount and currency are what the ledger records.
"""
import hmac
from html import escape

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app import config, daraja, payments, paypal

router = APIRouter(tags=["payments"])

_ACK = {"ResultCode": 0, "ResultDesc": "Accepted"}


@router.post("/api/payments/daraja/callback/{token}")
async def daraja_callback(token: str, request: Request):
    expected = config.DARAJA_CALLBACK_TOKEN
    if not expected or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Body must be JSON")
    outcome = daraja.parse_callback(body)
    if outcome is None:
        raise HTTPException(status_code=400, detail="Not an STK callback")
    payments.apply_mpesa_outcome(outcome)
    # Acknowledged whether or not anything changed: an unknown or already-final
    # payment is not an error Safaricom can fix by retrying.
    return _ACK


def _page(title: str, message: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{escape(title)}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:32rem;margin:15vh auto;"
        "padding:0 1rem;color:#222;background:#fff}h1{font-size:1.4rem}</style></head><body>"
        f"<h1>{escape(title)}</h1><p>{escape(message)}</p>"
        "<p>You can close this page and return to the chat.</p></body></html>")


_RESULT_TEXT = {
    payments.PAID: ("Payment received", "Your card payment was successful."),
    payments.FAILED: ("Payment not completed", "Your card was not charged."),
    payments.CANCELLED: ("Payment cancelled", "Nothing was charged."),
    payments.EXPIRED: ("Payment expired", "Nothing was charged. Ask the assistant for the fee again."),
    payments.NEEDS_REVIEW: ("Payment under review",
                            "We received a payment that needs checking. Our team will follow up."),
    payments.PENDING: ("Payment processing",
                       "We are still waiting for confirmation. The chat will show the result."),
}


@router.get("/payment/return")
def paypal_return(token: str = ""):
    row = payments.find_by_checkout("card", token) if token else None
    if row is None:
        return _page("Payment not found", "We could not find this checkout.")
    if row["status"] == payments.PENDING:
        payments.apply_card_outcome(token, paypal.capture_order(token))
        row = payments.find_by_checkout("card", token)
    title, message = _RESULT_TEXT.get(row["status"], ("Payment status unknown", ""))
    return _page(title, message)


@router.get("/payment/cancel")
def paypal_cancel(token: str = ""):
    row = payments.find_by_checkout("card", token) if token else None
    if row is None:
        return _page("Payment not found", "We could not find this checkout.")
    if row["status"] == payments.PENDING:
        # Confirm with PayPal first: a cancel link opened after paying must
        # not mark a settled payment as cancelled.
        outcome = paypal.get_order_outcome(token)
        if outcome.get("status"):
            payments.apply_card_outcome(token, outcome)
        else:
            payments.transition(row["id"], payments.CANCELLED,
                                detail="Guest cancelled at the checkout page.")
        row = payments.find_by_checkout("card", token)
    title, message = _RESULT_TEXT.get(row["status"], ("Payment status unknown", ""))
    return _page(title, message)
