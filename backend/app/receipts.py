"""
Downloadable PDF receipts for confirmed payments.

A receipt is issued only for a payment the ledger records as paid, and only to
the session that made it (or to signed-in staff). It shows what a passenger or
an examiner needs to check a payment against an M-Pesa statement: reference,
M-Pesa receipt number, amount, fee, time, and the phone number with most
digits hidden.

It is deliberately labelled as coming from the KQ-Propel research prototype
and not from Kenya Airways, and a sandbox payment is marked as a test, so the
document cannot be mistaken for an airline receipt.
"""
from datetime import datetime, timedelta, timezone
from typing import Dict

from fpdf import FPDF

from app import config, payments, privacy

EAT = timezone(timedelta(hours=3), "EAT")
RED, INK, SOFT, LINE = (200, 16, 46), (43, 13, 18), (107, 84, 87), (236, 207, 212)


class NotReceiptable(Exception):
    """The payment exists but has not been confirmed as paid."""


def _when(stamp: str) -> str:
    try:
        moment = datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc).astimezone(EAT)
        return moment.strftime("%d %b %Y, %H:%M:%S EAT")
    except (TypeError, ValueError):
        return stamp or "-"


def _ascii(text) -> str:
    """The built-in PDF fonts are Latin-1; keep every field printable."""
    return str(text if text is not None else "-").encode("latin-1", "replace").decode("latin-1")


def build(row: Dict) -> bytes:
    if row.get("status") != payments.PAID:
        raise NotReceiptable(row.get("status"))
    test = row.get("method") == "mpesa" and config.DARAJA_SANDBOX
    test = test or str(row.get("source") or "").endswith("sandbox")
    amount = payments.from_minor(row.get("amount_minor"))
    currency = row.get("currency") or "KES"

    pdf = FPDF(format="A5")
    pdf.set_auto_page_break(False)
    pdf.add_page()
    pdf.set_margins(12, 12, 12)

    pdf.set_fill_color(*RED)
    pdf.rect(0, 0, 148, 26, style="F")
    pdf.set_xy(12, 7)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 17)
    pdf.cell(0, 8, "KQ-Propel", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 5, "Payment receipt", new_x="LMARGIN", new_y="NEXT")

    pdf.set_y(32)
    if test:
        pdf.set_fill_color(255, 247, 224)
        pdf.set_draw_color(232, 199, 102)
        pdf.set_text_color(122, 85, 0)
        pdf.set_font("Helvetica", "B", 9)
        pdf.multi_cell(0, 5, "TEST PAYMENT - Safaricom Daraja sandbox. Paid to Safaricom's test Paybill, "
                             "not to Kenya Airways.", border=1, fill=True, padding=2,
                       new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)

    pdf.set_text_color(*SOFT)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 5, "Amount paid", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*INK)
    pdf.set_font("Helvetica", "B", 24)
    pdf.cell(0, 11, f"{currency} {amount:,.2f}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    fields = [
        ("Status", "Paid - confirmed by the provider"),
        ("Paid for", row.get("description")),
        ("Payment method", "M-Pesa (STK Push)" if row.get("method") == "mpesa" else "Card"),
        ("M-Pesa receipt" if row.get("method") == "mpesa" else "Provider receipt",
         row.get("provider_receipt") or "not yet received from the provider"),
        ("KQ-Propel reference", row.get("reference")),
        ("Paid from", privacy.mask_phone(row.get("phone_number")) if row.get("phone_number") else "-"),
        ("Requested", _when(row.get("created_at"))),
        ("Confirmed", _when(row.get("updated_at"))),
        # The checkout ID ends with the payer's number; staff can download
        # receipts too, so it is masked like the number above.
        ("Checkout ID", privacy.mask_checkout_id(row.get("checkout_request_id"))),
    ]
    pdf.set_draw_color(*LINE)
    for label, value in fields:
        y = pdf.get_y()
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*SOFT)
        pdf.cell(42, 7, label)
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(*INK)
        pdf.multi_cell(0, 7, _ascii(value), new_x="LMARGIN", new_y="NEXT")
        pdf.line(12, pdf.get_y(), 136, pdf.get_y())
        if pdf.get_y() - y > 14:
            pdf.ln(1)

    pdf.set_y(-34)
    pdf.set_font("Helvetica", "", 7.5)
    pdf.set_text_color(*SOFT)
    pdf.multi_cell(0, 4, "Issued by KQ-Propel, an agentic support assistant built as a Strathmore University "
                         "research prototype. This is not a Kenya Airways document. Check the M-Pesa receipt "
                         "number against your M-Pesa statement.", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 4, "Generated " + datetime.now(EAT).strftime("%d %b %Y, %H:%M EAT"))
    return bytes(pdf.output())


def filename(row: Dict) -> str:
    return f"KQ-Propel-receipt-{row.get('reference') or 'payment'}.pdf"
