"""
ReAct (Reasoning -> Action -> Observation) agent orchestrator.

Implements the Thought -> Action -> Observation loop described in the
proposal: the agent classifies the passenger's intent and tone, decides
whether it needs to call a tool (policy retrieval, live flight telemetry, or
an M-Pesa STK push payment), executes it, observes the result, and only then
composes a final grounded answer. Every step is captured in a transparent
trace so the chat UI and admin dashboard can display the agent's reasoning,
supporting the groundedness / zero-hallucination goals.
"""
import re
import uuid
import json
from typing import Dict, List, Optional, Tuple

from app import (aviationstack, booking, composer, config, daraja, database, evaluation, ratelimit,
                  handover, kq_flights, memory, payments, paypal, retrieval, sentiment,
                  service_requests, swahili)
from app.llm import generate_answer
from app.vectorstore import get_vector_store

_FLIGHT_RE = re.compile(r"\bKQ\s?-?\s?(\d{2,4})\b", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?:\+?254|0)(7\d{8}|1\d{8})")
_AMOUNT_RE = re.compile(r"(?:ksh|kes)\s?([\d,]+)", re.IGNORECASE)

_GREETING_RE = re.compile(
    r"^\s*(hi|hey|hello|hallo|helo|habari|jambo|niaje|good\s*(morning|afternoon|evening)|"
    r"how\s+are\s+you|what'?s\s+up)\b(\s+(there|team|guys))?[\s!.,?]*$", re.IGNORECASE)
_THANKS_RE = re.compile(r"^\s*(thanks|thank\s*you|asante|much\s+appreciated|appreciate\s+it|"
                         r"thx|ta)\b(\s+(a\s+lot|so\s+much|very\s+much|again|"
                         r"for\s+(your|the|all)\s+\w+(\s+\w+)?|for\s+\w+))?[\s!.,?]*$", re.IGNORECASE)
_BYE_RE = re.compile(r"^\s*(bye|goodbye|see\s+you|cheers|that'?s\s+all|no\s+thanks?)\b[\s!.,?]*$",
                      re.IGNORECASE)
# Anchored to the whole message: "help me" alone is a capability request, but
# "help me with an overweight fee for 10kg" is a real question that must go
# through retrieval.
_HELP_RE = re.compile(
    r"^\s*(?:hi|hello|hey)?[\s,]*"
    r"(?:what\s+can\s+you\s+do|who\s+are\s+you|what\s+are\s+you|how\s+do\s+you\s+work|"
    r"what\s+do\s+you\s+do|what\s+are\s+your\s+capabilities|capabilities|"
    r"(?:can|could|would)\s+you\s+help\s+me|help\s+me|help)"
    r"[\s!.,?]*$", re.IGNORECASE)

# Transactional phrasing actually initiates an STK push; informational
# phrasing ("how do I pay?") is answered from policy instead, so the agent
# never charges a passenger who only asked a question.
_TRANSACTIONAL_RE = re.compile(
    r"\b(pay\s+now|pay\s+it|pay\s+the\s+fee|pay\s+for\s+it|pay\s+this|make\s+the\s+payment|"
    r"send\s+(?:me\s+)?(?:the\s+)?(?:stk|prompt|push|request)|initiate\s+(?:the\s+)?payment|"
    r"charge\s+me|(?:can|could|may)\s+i\s+pay|i\s+(?:want|would\s+like|wish|need)\s+to\s+pay|"
    r"i'?d\s+like\s+to\s+pay|let'?s\s+pay|go\s+ahead\s+and\s+pay|settle\s+(?:it|the\s+fee)|"
    r"pay\s+(?:ksh|kes)\.?\s?[\d,]+|process\s+(?:the\s+)?payment)\b", re.IGNORECASE)
# The empathy prefix fires above this score, not at the 0.5 decision
# boundary, so a marginal call does not open with an apology to a passenger
# who merely asked a question. Named because it is applied in two places.
EMPATHY_THRESHOLD = 0.55

# Share of a question's subject words that the retrieved sections must
# contain, and that the policy vocabulary must contain, before the agent
# answers from them. Tuned with
# tools/test_grounding_guard.py against the evaluation set (every question must
# pass) and off-topic probes (every probe must be refused).
GROUNDING_MIN_COVERAGE = 0.5
# Cosine similarity the best retrieved section needs when only half a
# question's subject words occur in the policies. Every off-topic probe in
# tools/test_grounding_guard.py scores below 0.30 with all-MiniLM-L6-v2; the
# answerable borderline questions score above 0.40.
GROUNDING_BORDERLINE_MIN_SIMILARITY = 0.33

_PAYMENT_TOPIC_RE = re.compile(r"\b(pay|payment|m-?pesa|mpesa|stk|till|settle|card|visa)\b",
                                re.IGNORECASE)

# Choosing the card rail. Includes the situation that motivated it: a guest
# saying they have no Kenyan number is choosing a rail, not making small talk,
# and must not be answered with "give me your M-Pesa number" again.
_CARD_RE = re.compile(
    r"\b(card|cards|visa|mastercard|master\s?card|amex|american\s+express|"
    r"credit\s?card|debit\s?card|paypal|by\s+card|with\s+my\s+card|"
    r"no\s+(?:kenyan|safaricom|mpesa|m-pesa)\s+(?:number|line|sim)|"
    r"(?:don'?t|do\s+not|doesn'?t)\s+have\s+(?:a\s+)?(?:kenyan|safaricom|mpesa|m-pesa)|"
    r"not\s+(?:in\s+)?kenyan?|outside\s+kenya|international\s+(?:number|card))\b",
    re.IGNORECASE)
# Explicitly choosing the mobile-money rail when both are on the table.
_MPESA_RE = re.compile(r"\b(m-?pesa|mpesa|stk|mobile\s+money|phone|my\s+number)\b",
                        re.IGNORECASE)

# Short confirmations/declines only carry meaning while a payment offer is
# outstanding, so they are evaluated against the stored payment state.
_AFFIRM_RE = re.compile(
    r"^\s*(yes|yeah|yep|yup|sure|ok|okay|alright|please|please\s+do|yes\s+please|"
    r"go\s+ahead|do\s+it|send\s+it|send\s+the\s+prompt|proceed|continue|sawa|ndio|"
    r"pay|pay\s+now|let'?s\s+do\s+it)\b[\s!.,?]*$", re.IGNORECASE)
_DECLINE_RE = re.compile(
    r"^\s*(no|nope|not\s+now|not\s+yet|later|maybe\s+later|cancel|stop|"
    r"no\s+thanks?|no\s+thank\s+you)\b[\s!.,?]*$", re.IGNORECASE)
_FEE_TOPIC_RE = re.compile(r"\b(fee|fees|charge|charges|cost|costs|price|how\s+much|"
                            r"pay|payment|owe|balance)\b", re.IGNORECASE)
# Whether a quoted amount is money the passenger *owes* (payable) or money owed
# *to* them (an entitlement). Only the former may be offered for settlement.
_PAYABLE_RE = re.compile(r"\b(fee|fees|charge|charged|charges|surcharge|payable|"
                          r"cost|costs|costing|excess|supplementary|incur|incurs|"
                          r"settle|settled)\b", re.IGNORECASE)
_ENTITLEMENT_RE = re.compile(r"\b(entitled|entitlement|voucher|compensation|compensated|"
                              r"refund|refunds|refunded|reimburse|reimbursed|claim|"
                              r"allowance\s+of|credit|deducted|less\s+any)\b", re.IGNORECASE)
# Published Kenya Airways fees are stated in US dollars, and some fee tables
# carry no currency label at all. An amount may only be pushed to M-Pesa when
# the source states Kenyan shillings: Daraja settles in KES, so treating a
# "USD 75" fee as 75 shillings - or converting it at an invented rate - would
# charge the passenger an amount no source supports.
_MONEY_RE = re.compile(
    r"Ksh\s?([\d,]+)"
    r"|(?:USD|US\$|\$)\s?([\d,]+)"
    r"|([\d,]+)\s+US\s+dollars",
    re.IGNORECASE,
)

# Departure/arrival *times* are live operational data, not policy. The agent
# has a telemetry tool for this, but it is keyed on a flight number - without
# one it must ask, rather than answering from an unrelated policy section.
_SCHEDULE_RE = re.compile(
    r"\b(?:what\s+time|when|which\s+time)\b[^?]{0,60}?\b(?:depart|departs|departing|departure|"
    r"leave|leaves|leaving|take[\s-]?off|takes[\s-]?off|taking[\s-]?off|takeoff|board|boards|"
    r"boarding|arrive|arrives|arriving|arrival|land|lands|landing|fly|flies)\b"
    r"|\b(?:departure|arrival|boarding)\s+time\b"
    r"|\bflight\s+(?:time|times|schedule|timetable)\b"
    r"|\bis\s+(?:my|the)\s+flight\s+(?:on\s+time|delayed|leaving|departing)\b",
    re.IGNORECASE)
_ROUTE_RE = re.compile(r"\bfrom\s+([A-Za-z][A-Za-z\s]{2,20}?)\s+to\s+([A-Za-z][A-Za-z\s]{2,20}?)"
                        r"(?=[\s,.?!]|$)", re.IGNORECASE)

OUT_OF_SCOPE_REPLY = (
    "I don't have anything on that in the airline's policy documents, so I'd rather "
    "not guess.\n\n"
    "I can help with baggage allowances and overweight fees, flight delays and "
    "compensation, refunds and ticket changes, check-in and boarding rules, live "
    "flight status, and paying fees by M-Pesa. Could you rephrase your question "
    "around one of those?"
)

GREETING_REPLY = (
    "Hello, and welcome to KQ-Propel. I'm your aviation support assistant.\n\n"
    "I can help you with:\n"
    "- Baggage allowances, overweight fees and lost baggage claims\n"
    "- Flight status, delays and compensation entitlements\n"
    "- Refunds, ticket changes and name corrections\n"
    "- Check-in times, boarding and travel documents\n"
    "- Paying ancillary fees by M-Pesa, right here in the chat\n\n"
    "What can I help you with today?"
)

CAPABILITY_REPLY = (
    "I'm KQ-Propel, an AI aviation support assistant. I answer using the airline's "
    "official policy documents, so every answer is grounded in a real policy section.\n\n"
    "I can look up baggage and overweight fees, flight delay compensation, refund and "
    "ticket-change rules, and check-in requirements. I can also check live flight status "
    "(try asking about flight KQ100) and send you an M-Pesa payment prompt for fees.\n\n"
    "Just ask your question in your own words."
)

THANKS_REPLY = "You're very welcome. Is there anything else I can help you with?"
BYE_REPLY = "Safe travels, and thank you for flying with us. I'm here if you need anything else."

SYSTEM_PROMPT = (
    "You are KQ-Propel, an aviation passenger-support assistant. Answer strictly "
    "and only using the CONTEXT provided (retrieved from official policy "
    "documents) and any TOOL OBSERVATIONS supplied. Never invent fees, dates or "
    "policy details that are not present in the context. If the context does not "
    "contain the answer, say so plainly. Be concise, empathetic, and cite the "
    "relevant policy section."
)


def _detect_flight_number(message: str) -> Optional[str]:
    match = _FLIGHT_RE.search(message)
    return f"KQ{match.group(1)}" if match else None


def _detect_route(message: str) -> Optional[str]:
    match = _ROUTE_RE.search(message)
    if not match:
        return None
    origin = " ".join(match.group(1).split()).title()
    destination = " ".join(match.group(2).split()).title()
    return f"{origin} to {destination}"


def _schedule_request_reply(message: str) -> str:
    """Asked for a departure/arrival time without a flight number. The honest
    answer is to request the flight number rather than quote a policy."""
    route = _detect_route(message)
    opening = (
        f"I can check that for you. I look up departure and arrival times from the live "
        f"flight telemetry feed rather than a printed timetable, so I need your flight "
        f"number"
    )
    if route:
        opening += f" for the {route} service"
    return (
        opening + ".\n\n"
        "Send it through in the form KQ100 and I'll pull up the current status, the gate "
        "and the latest estimated departure time straight away. You'll find it on your "
        "booking confirmation or boarding pass."
    )


# Words that only ask where a flight is. A question made of nothing else
# ("What is the status of KQ100?") has no policy subject, yet "status" matches
# headings such as "Status of This Document", so the answer used to carry an
# unrelated policy sentence after the flight status.
_FLIGHT_STATUS_WORDS = frozenset({
    "what", "status", "flight", "flights", "delayed", "delay", "late", "early", "time",
    "today", "now", "depart", "departs", "departed", "departure", "departing", "arrive",
    "arrives", "arrived", "arrival", "arriving", "land", "landed", "landing", "gate",
    "schedule", "scheduled", "cancelled", "canceled", "eta", "kq",
})


def _is_flight_status_only(question: str) -> bool:
    """True when every subject word in the question is about a flight's state."""
    words = [t for t in retrieval.topical_tokens(question)
             if not re.fullmatch(r"(?:kq)?\d{1,4}|kq\d{1,4}", t)]
    return all(w in _FLIGHT_STATUS_WORDS for w in words)


def _is_grounded_in_context(message: str, chunks: List[Dict]) -> bool:
    """True when the retrieved policy text actually shares subject matter with
    the question. Retrieval always returns its best candidates, so without this
    check an off-topic question is answered from whichever section ranked
    highest - which is how a departure-time question ended up being answered
    with refund processing timelines.

    Only words that carry subject matter are weighed (`topical_tokens` drops
    filler words and place names), and only those the policy corpus could
    possibly contain. A word the corpus never uses ("nobody", "weather") says
    nothing about whether a retrieved section is relevant, so it neither helps
    nor hurts. Each word also matches through its synonyms, so a "bag" question
    is supported by a "baggage" section.

    Two failures motivated this form. "My bag has been missing for three days
    and NOBODY is helping me!! What can I claim?" was refused although the
    delayed-baggage sections were retrieved, because filler words diluted the
    overlap below the old 30% threshold. "What is the weather in Nairobi?" was
    answered, because "nairobi" alone matched a baggage section.
    """
    vocab = retrieval.corpus_vocabulary()

    def forms(token: str) -> set:
        return set(retrieval.expand_query([token]))

    topical = list(dict.fromkeys(retrieval.topical_tokens(message)))
    evidence = [t for t in topical if forms(t) & vocab]
    # "What is the fee for a pet?": "fee" is in the policies and "pet" is not,
    # which is exactly half and was answered with the booking fees. A word
    # such as "fee" or "allowed" says what kind of answer is wanted, not what
    # it is about, so it cannot be the only evidence when the question does
    # name a subject.
    subject = [t for t in topical if t not in _VAGUE_WORDS]
    if subject and not any(t not in _VAGUE_WORDS for t in evidence):
        return False
    # A question mostly about things the policies never mention ("exchange
    # rate of the shilling", "translate hello") is off-topic even when one of
    # its words happens to appear somewhere in the corpus.
    # More than half, not half: "book me a taxi to my hotel" and "vaccinations
    # to travel to Ghana" each had exactly half their subject words in the
    # corpus ("airport", "hotel"; "travel") and were answered from unrelated
    # sections.
    share = len(evidence) / len(topical) if evidence else 0.0
    if share < GROUNDING_MIN_COVERAGE:
        return False
    # At exactly the threshold the words alone cannot tell "how do I trace a
    # bag and who do I call?" (answerable) from "book me a taxi to my hotel"
    # (not): each has half its subject words in the policies. The semantic
    # similarity of the best section decides those borderline cases.
    if share <= GROUNDING_MIN_COVERAGE and len(topical) > 1:
        best = max((c.get("dense_score") or 0.0 for c in chunks), default=0.0)
        if best < GROUNDING_BORDERLINE_MIN_SIMILARITY:
            return False
    # Coverage is judged across all retrieved sections together: a multi-part
    # question ("when must I be at the airport and what will the surfboard
    # cost?") is answered by two documents, neither of which covers it alone.
    retrieved_tokens = set()
    for chunk in chunks:
        retrieved_tokens.update(retrieval.tokenize(
            chunk.get("search_text") or chunk.get("text") or ""))
    hits = sum(1 for t in evidence if forms(t) & retrieved_tokens)
    return hits / len(evidence) >= GROUNDING_MIN_COVERAGE


def _normalize_phone(raw_match) -> str:
    return "254" + raw_match.group(1)


def _distinct_amounts(text: str) -> List[Tuple[float, str]]:
    """Returns each distinct money amount in the text with its stated currency.

    Unlabelled figures are deliberately ignored. The published heavy-bag table
    gives bare numbers with no currency, and guessing one would put an
    unsupported amount in front of the passenger.
    """
    seen: List[Tuple[float, str]] = []
    for kes, usd_prefix, usd_suffix in _MONEY_RE.findall(text or ""):
        raw = kes or usd_prefix or usd_suffix
        currency = "KES" if kes else "USD"
        value = float(raw.replace(",", ""))
        if (value, currency) not in seen:
            seen.append((value, currency))
    return seen


def _new_reference() -> str:
    # Daraja accepts at most 12 characters of account reference.
    return f"KQ{uuid.uuid4().hex[:8].upper()}"


def _existing_attempt_result(attempt: Dict) -> Dict:
    """A payment result describing an attempt already in progress, returned
    instead of charging the passenger a second time."""
    return {
        "success": True, "duplicate": True, "source": attempt.get("source"),
        "status": attempt.get("status"), "method": attempt.get("method"),
        "currency": attempt.get("currency"),
        "amount": payments.from_minor(attempt.get("amount_minor")),
        "phone_number": attempt.get("phone_number"), "reference": attempt.get("reference"),
        "checkout_request_id": attempt.get("checkout_request_id"),
    }


def _execute_payment(session_id: str, phone: str, amount: float, description: str,
                      trace: List[Dict], quote_id: Optional[str] = None) -> Dict:
    """Sends an STK push for a quoted fee and records it in the ledger.

    If the quote already has a payment in progress, that payment is returned
    and nothing is sent: a repeated "yes" or a retried request must never
    charge twice.
    """
    existing = payments.open_attempt(quote_id)
    if existing:
        trace.append({"step": "thought",
                       "content": f"Payment {existing['reference']} for this quote is already "
                                  f"{existing['status']}; not sending a second prompt."})
        return _existing_attempt_result(existing)

    # Each prompt rings a real phone, and anyone can type any number, so
    # prompts are limited per conversation and per number.
    try:
        ratelimit.hit("mpesa_session", session_id)
        ratelimit.hit("mpesa_phone", daraja.normalize_phone(phone) or phone)
    except ratelimit.Limited as exc:
        trace.append({"step": "thought",
                       "content": f"M-Pesa prompt refused by the rate limit ({exc.bucket})."})
        return {"success": False, "rate_limited": True,
                "message": ("I've sent several M-Pesa prompts in the last few minutes, so I'll "
                            f"wait before sending another. Please try again in "
                            f"{ratelimit.wait_text(exc)}, or say \"card\" to pay by card instead. "
                            "Nothing has been charged.")}

    reference = _new_reference()
    result = daraja.initiate_stk_push(
        phone_number=phone, amount=amount, reference=reference, description=description,
    )
    trace.append({"step": "action", "tool": "initiate_mpesa_stk_push",
                   "input": {"phone": phone, "amount": amount, "reference": reference},
                   "observation": result})
    charged = result.get("amount", amount)
    row, created = payments.record_attempt(
        session_id=session_id, quote_id=quote_id, method="mpesa", currency="KES",
        amount=charged, result=result, phone_number=phone, reference=reference,
        description=description)
    if not created:
        return _existing_attempt_result(row)
    if result.get("success"):
        database.clear_pending_payment(session_id)
    result.update({"amount": charged, "phone_number": phone, "reference": reference,
                   "method": "mpesa", "currency": "KES", "status": row["status"]})
    return result


def _execute_card_payment(session_id: str, amount: float, currency: str,
                           description: str, trace: List[Dict],
                           quote_id: Optional[str] = None) -> Dict:
    """Creates a card checkout for the fee in the currency it is published in.

    Nothing is converted here. That is the point of the rail: the dollar fees
    Kenya Airways publishes can be charged as dollars, so the guest is never
    quoted a figure that depends on this prototype's exchange-rate assumption.
    """
    existing = payments.open_attempt(quote_id)
    if existing:
        trace.append({"step": "thought",
                       "content": f"Payment {existing['reference']} for this quote is already "
                                  f"{existing['status']}; not creating a second checkout."})
        return _existing_attempt_result(existing)

    reference = _new_reference()
    result = paypal.create_card_order(
        amount=amount, currency=currency, reference=reference, description=description)
    trace.append({"step": "action", "tool": "create_card_checkout",
                   "input": {"amount": amount, "currency": currency,
                             "reference": reference},
                   "observation": result})
    row, created = payments.record_attempt(
        session_id=session_id, quote_id=quote_id, method="card", currency=currency,
        amount=amount, result=result, reference=reference, description=description)
    if not created:
        return _existing_attempt_result(row)
    if result.get("success"):
        database.clear_pending_payment(session_id)
    result.update({"reference": reference, "method": "card", "status": row["status"]})
    return result


def _duplicate_payment_message(result: Dict) -> str:
    if result.get("status") == payments.PAID:
        return (f"That fee has already been paid (reference {result.get('reference')}). "
                "You won't be charged again.")
    rail = "M-Pesa prompt" if result.get("method") == "mpesa" else "card checkout"
    return (f"A {rail} for this fee is already in progress (reference "
            f"{result.get('reference')}), so I haven't started another one. Complete or "
            "cancel that one first - its status is shown below.")


def _payment_sent_message(amount: float, phone: str, result: Optional[Dict] = None) -> str:
    if result and result.get("duplicate"):
        return _duplicate_payment_message(result)
    if result and result.get("source") == payments.SIMULATED:
        reason = (" M-Pesa is in test mode, and test prompts are only sent to registered test "
                  "numbers." if result.get("sandbox_blocked") else "")
        return (f"Demonstration mode: this is where an M-Pesa prompt for **Ksh {amount:,.0f}** "
                f"would be sent to {phone}. No real payment request was made, and nothing "
                f"will be charged.{reason}")
    if result and result.get("sandbox_test_fee"):
        return (
            f"Test mode: I've sent an M-Pesa prompt for **Ksh {result.get('amount', 1):,.0f}** to "
            f"{phone}, standing in for the Ksh {amount:,.0f} fee.\n\n"
            "This is Safaricom's test system, but the shilling is real: entering your PIN pays "
            "it to Safaricom's test Paybill. Safaricom may reverse it, but that is not guaranteed. The status below updates "
            "once Safaricom confirms the outcome."
        )
    return (
        f"I've sent an M-Pesa payment prompt for **Ksh {amount:,.0f}** to {phone}.\n\n"
        "Check your phone: the request should appear within a few seconds. Enter your "
        "M-Pesa PIN to authorise it. The payment status below updates once Safaricom "
        "confirms the outcome - the fee isn't settled until it shows as paid."
    )


def _card_checkout_message(amount: float, currency: str, result: Dict) -> str:
    if result.get("duplicate"):
        return _duplicate_payment_message(result)
    simulated = result.get("source") == payments.SIMULATED
    link = result.get("approval_url") or ""
    msg = (
        f"Here's your secure card checkout for **{currency} {amount:,.2f}**.\n\n"
        f"{link}\n\n"
        "Visa, Mastercard and American Express are accepted, and you don't need a PayPal "
        "account - choose the card option on that page. You're charged in "
        f"{currency}, the currency the fee is published in, so there's no conversion on "
        "our side; your bank may apply its own if your card is in another currency."
    )
    if simulated:
        msg += ("\n\n*(Demonstration mode: no card gateway credentials are configured, so "
                "that link is a simulated checkout rather than a live one.)*")
    return msg


def _ask_for_phone_message(amount: float, currency: str, kes_amount: float,
                            conversion_note: Optional[str]) -> str:
    """Asked when the guest has chosen M-Pesa but has not given a number yet.

    The shilling figure is quoted because that is what Safaricom will actually
    prompt for, but the published figure and the conversion note travel with it
    so the guest is never shown a converted amount without being told it is one.
    """
    lines = [f"Happy to settle that over M-Pesa. The amount due is "
             f"**{currency} {amount:,.2f}**."]
    if conversion_note:
        lines.append(conversion_note)
        lines.append(f"So the prompt will be for **Ksh {kes_amount:,.0f}**.")
    lines.append(
        "What's the M-Pesa number I should send the payment prompt to? You can give it as "
        "07XX XXX XXX or 2547XX XXX XXX. It needs to be a Kenyan Safaricom line - if you "
        "don't have one, say \"card\" instead and I'll generate a checkout link."
    )
    return "\n\n".join(lines)


def _ask_for_method_message(amount: float, currency: str) -> str:
    """Asked after the guest agrees to pay but before a rail is known."""
    lines = [f"Happy to help you settle that. The amount due is **{currency} {amount:,.2f}**.",
             "",
             "How would you like to pay?",
             "",
             "- **Card** - say \"card\" and I'll generate a secure checkout link. Visa, "
             "Mastercard and American Express, no PayPal account needed."]
    if currency == "KES":
        lines.append("- **M-Pesa** - reply with your Kenyan number (07XX XXX XXX or "
                     "2547XX XXX XXX) and I'll push the prompt to your phone.")
    else:
        kes_amount, _ = _to_kes(amount, currency)
        lines.append(f"- **M-Pesa** - reply with your Kenyan number (07XX XXX XXX). M-Pesa "
                     f"settles in shillings, so that would be about "
                     f"**Ksh {kes_amount:,.0f}** at this assistant's indicative rate of "
                     f"{config.USD_TO_KES_RATE:,.2f} KES/USD, which is a prototype setting "
                     f"rather than a published airline figure.")
        lines.append("")
        lines.append("Paying by card avoids that conversion entirely, since the fee is "
                     f"published in {currency}.")
    return "\n".join(lines)


def _offer_payment_message(amount: float, currency: str,
                            kes_amount: Optional[float]) -> str:
    """Offers both rails.

    M-Pesa is listed second and qualified, because an STK push can only reach a
    Kenyan mobile number - offering it first to a guest flying from Amsterdam
    would be offering something they cannot use.
    """
    lines = [f"\n\nWould you like to settle the **{currency} {amount:,.2f}** now? "
             "There are two ways:"]
    lines.append(f"\n\n- **Card** - I'll generate a secure checkout link and you pay the "
                 f"{currency} {amount:,.2f} directly. Just say \"card\".")
    if kes_amount is not None:
        if currency == "KES":
            lines.append("\n- **M-Pesa** - reply with your Kenyan number (for example "
                         "0712345678) and I'll push the prompt to your phone.")
        else:
            lines.append(f"\n- **M-Pesa** - available on a Kenyan mobile number. M-Pesa "
                         f"settles in shillings, so this would be **Ksh {kes_amount:,.0f}** "
                         f"at the indicative rate below. Reply with your number "
                         f"(for example 0712345678).")
    return "".join(lines)


def _to_kes(amount: float, currency: str) -> Tuple[float, Optional[str]]:
    """Converts a fee into the shillings Daraja settles in.

    Returns the shilling amount and, when a conversion happened, the sentence
    disclosing it. Kenya Airways publishes these fees in dollars and states no
    exchange rate, so the rate used is the prototype's own declared constant -
    the passenger is told the rate rather than shown a shilling figure that
    appears to come from the airline.
    """
    if currency == "KES":
        return amount, None
    rate = config.USD_TO_KES_RATE
    converted = round(amount * rate)
    note = (
        f"The published charge is USD {amount:,.0f}. Kenya Airways does not publish a "
        f"shilling equivalent, so I've converted it at this assistant's configured "
        f"indicative rate of {rate:,.2f} KES per USD, which gives Ksh {converted:,.0f}. "
        f"That rate is a prototype setting, not a live exchange rate - the amount your "
        f"bank or the airline finally applies may differ."
    )
    return float(converted), note


def _handle_pending_payment(session_id: str, message: str, pending: Dict,
                             trace: List[Dict], sentiment_result: Dict) -> Optional[Dict]:
    """Continues a payment conversation that is already in progress. Returns a
    finished turn, or None if the passenger has moved on to something else."""
    amount = float(pending["amount"])
    currency = (pending.get("currency") or "KES").upper()
    description = pending.get("description") or "KQ-Propel ancillary fee settlement"
    quote_id = pending.get("quote_id")

    # A quote is only good for PAYMENT_QUOTE_TTL_MINUTES. Settling an old one
    # could charge a fee the policy no longer states.
    if payments.quote_expired(pending):
        database.clear_pending_payment(session_id)
        replying_to_offer = bool(_AFFIRM_RE.match(message) or _PHONE_RE.search(message)
                                 or _CARD_RE.search(message) or _MPESA_RE.search(message)
                                 or _TRANSACTIONAL_RE.search(message))
        if not replying_to_offer:
            return None
        trace.append({"step": "thought", "content": "Payment offer has expired; not settling it."})
        return _finalize_direct(
            session_id, message,
            "That payment offer has expired, so I haven't charged anything. Ask me about "
            "the fee again and I'll quote it afresh from the current policy.",
            trace, sentiment_result)

    if _DECLINE_RE.match(message):
        database.clear_pending_payment(session_id)
        trace.append({"step": "thought", "content": "Passenger declined the payment offer."})
        return _finalize_direct(
            session_id, message,
            "No problem - I won't take any payment. Let me know if you'd like to "
            "pay later, or if there's anything else I can help you with.",
            trace, sentiment_result)

    # A phone number is an unambiguous choice of the M-Pesa rail, so it is read
    # before the card check - "pay with my card on 0712345678" is incoherent,
    # but a number is the more specific signal.
    phone_match = _PHONE_RE.search(message)
    if phone_match:
        phone = _normalize_phone(phone_match)
        kes_amount, conversion_note = _to_kes(amount, currency)
        trace.append({"step": "thought",
                       "content": (f"M-Pesa number supplied for the outstanding "
                                   f"{currency} {amount:,.2f} payment. "
                                   f"Pushing Ksh {kes_amount:,.0f}.")})
        result = _execute_payment(session_id, phone, kes_amount, description, trace,
                                   quote_id=quote_id)
        if result.get("success"):
            answer = _payment_sent_message(kes_amount, phone, result)
            if conversion_note and not result.get("duplicate"):
                answer = conversion_note + "\n\n" + answer
        elif result.get("rate_limited"):
            answer = result["message"]
        else:
            answer = ("I wasn't able to send the M-Pesa prompt just now. Please try again in "
                      "a moment, or say \"card\" and I'll generate a secure card checkout "
                      "instead.")
        return _finalize_direct(session_id, message, _apply_empathy(answer, sentiment_result),
                                 trace, sentiment_result, payment=result)

    if _CARD_RE.search(message):
        trace.append({"step": "thought",
                       "content": (f"Passenger chose the card rail for {currency} "
                                   f"{amount:,.2f}. No conversion is required - the card is "
                                   f"charged in the published currency.")})
        result = _execute_card_payment(session_id, amount, currency, description, trace,
                                        quote_id=quote_id)
        answer = (_card_checkout_message(amount, currency, result) if result.get("success")
                  else "I couldn't create the card checkout just now. Please try again in a "
                       "moment, or pay at the airport counter.")
        return _finalize_direct(session_id, message, _apply_empathy(answer, sentiment_result),
                                 trace, sentiment_result, payment=result)

    if _MPESA_RE.search(message):
        # The rail is chosen but the number is not known yet, so nothing is
        # pushed - we ask for the line first.
        kes_amount, conversion_note = _to_kes(amount, currency)
        database.update_pending_stage(session_id, "awaiting_phone")
        trace.append({"step": "thought",
                       "content": "Passenger chose M-Pesa but gave no number; asking for it."})
        return _finalize_direct(
            session_id, message,
            _apply_empathy(_ask_for_phone_message(amount, currency, kes_amount,
                                                   conversion_note), sentiment_result),
            trace, sentiment_result)

    if _AFFIRM_RE.match(message) or _TRANSACTIONAL_RE.search(message):
        database.update_pending_stage(session_id, "awaiting_method")
        trace.append({"step": "thought",
                       "content": "Payment confirmed; awaiting the passenger's choice of rail."})
        return _finalize_direct(session_id, message,
                                 _apply_empathy(_ask_for_method_message(amount, currency),
                                                sentiment_result),
                                 trace, sentiment_result)

    return None


def _amount_scopes(sentence: str) -> List[Tuple[set, Tuple[float, str]]]:
    """Splits a fee sentence into one scope per amount.

    Each amount's scope runs from the amount to the next one, so the qualifying
    words travel with the figure they qualify. Splitting on conjunctions
    instead leaves the sentence subject attached to the first amount only -
    in "correcting a name costs 15 USD on a domestic booking and 75 USD on an
    international booking", the word "name" would then appear to distinguish
    the 15 from the 75.
    """
    matches = list(_MONEY_RE.finditer(sentence))
    scopes: List[Tuple[set, Tuple[float, str]]] = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(sentence)
        span = sentence[match.start():end]
        amounts = _distinct_amounts(span)
        if len(amounts) == 1:
            scopes.append((set(retrieval.tokenize(span)), amounts[0]))
    return scopes


def _select_amount_by_context(sentence: str, message: str) -> Optional[Tuple[float, str]]:
    """Picks the fee that matches the passenger's stated situation.

    Published fee sentences almost always quote several figures at once -
    "costs 15 US dollars on a domestic booking and 75 US dollars on an
    international booking". Refusing to act on any of them makes the fee
    unusable; guessing between them could charge the wrong one. The figure
    whose qualifying words the passenger's own message matches is chosen, and
    only when exactly one matches, so an unspecific request still declines.
    """
    context = set(retrieval.tokenize(message))
    if not context:
        return None

    scopes = _amount_scopes(sentence)
    if len(scopes) < 2:
        return scopes[0][1] if scopes else None

    shared = set.intersection(*(tokens for tokens, _ in scopes))
    best: List[Tuple[int, Tuple[float, str]]] = []
    for tokens, amount in scopes:
        overlap = len(context & (tokens - shared))
        if overlap:
            best.append((overlap, amount))

    if not best:
        return None
    best.sort(key=lambda b: b[0], reverse=True)
    if len(best) > 1 and best[0][0] == best[1][0]:
        return None  # genuinely ambiguous - do not guess
    return best[0][1]


def _payable_fee(answer: str, chunks: List[Dict],
                  message: str = "") -> Optional[Tuple[float, str]]:
    """Returns the single fee the passenger owes and the currency it is stated
    in, if the answer quotes exactly one and it is genuinely a charge rather
    than money owed to them.

    Whether a payment can be offered depends on what the *answer* says, not on
    how the question happened to be phrased - "I have an overweight baggage by
    10kg" contains no fee words yet clearly warrants an offer to settle.
    """
    amounts = _distinct_amounts(answer)
    if not amounts:
        return None

    sentence = next((s for s in re.split(r"(?<=[.!?])\s+", answer) if _MONEY_RE.search(s)), "")
    if _ENTITLEMENT_RE.search(sentence):
        return None

    heading = chunks[0].get("section", "") if chunks else ""
    if not (_PAYABLE_RE.search(sentence) or _PAYABLE_RE.search(heading)):
        return None

    if len(amounts) == 1:
        return amounts[0]
    return _select_amount_by_context(sentence, message)


def _apply_empathy(answer: str, sentiment_result: Dict) -> str:
    """Sentiment-aware framing (corrections requirement): a frustrated
    passenger gets acknowledgement and an explicit escalation path."""
    if (sentiment_result["label"] != "frustrated"
            or sentiment_result["frustration_score"] < EMPATHY_THRESHOLD):
        return answer
    return (
        "I'm really sorry for the trouble - I understand how frustrating this is, "
        "and I'll help you sort it out right away.\n\n" + answer + "\n\n" + _escalation_offer()
    )


def _escalation_offer() -> str:
    """Offers a person only when someone is actually watching the handover
    queue; otherwise points to the airline's own contact route."""
    if config.HANDOVER_ENABLED:
        return ("If this doesn't fully resolve things, I can pass you to a member of our "
                "support team - just say \"speak to a person\".")
    return ("If this doesn't fully resolve things, you can reach a member of staff through "
            f"{config.HANDOVER_FALLBACK_CONTACT}.")


def _start_name_request(session_id: str, trace: List[Dict]) -> Optional[str]:
    """Starts (or continues) a name correction for the booking in memory.
    Returns the question to ask, or None when there is no booking yet."""
    trip = booking.remembered_booking(session_id)
    if not trip.get("segments"):
        return None
    names = trip.get("passengers") or []
    segments = trip["segments"]
    if not names:
        service_requests.start(session_id, service_requests.AWAITING_BOOKING)
        return ("To correct a name I need to look the booking up again. Please send the booking "
                "reference and surname, for example \"booking KQDLW1, surname Wangui\".")
    # The policy: name changes apply to Kenya Airways operated flights only.
    if not all(str(s.get("flight_number") or "").startswith("KQ") for s in segments):
        return ("Name changes apply to Kenya Airways operated flights only, and this booking includes "
                "a flight operated by another airline, so I can't take this request here. Please "
                "contact Kenya Airways customer service.")
    request = service_requests.start(session_id, service_requests.AWAITING_NEW_NAME,
                                     booking_reference=trip.get("reference"), current_name=names[0],
                                     scope=booking.scope(segments[0]))
    trace.append({"step": "thought",
                  "content": f"Name correction {service_requests.reference(request)} started for booking "
                             f"{trip.get('reference')}; asking for the new name."})
    return (f"Your booking {trip.get('reference')} shows the passenger as **{names[0]}**. "
            f"What should the name be? You can send the full name, or just the corrected surname.")


def _name_request_summary(request: Dict) -> str:
    what = ("a correction of up to three characters" if request["change_kind"] == "correction"
            else "a full name change")
    return (f"Here is the change I'll submit:\n\n"
            f"- Booking: **{request['booking_reference']}** ({request['scope']})\n"
            f"- Current name: **{request['current_name']}**\n"
            f"- New name: **{request['new_name']}**\n"
            f"- {request['characters_changed']} character(s) changed, which counts as {what}\n"
            f"- Fee: **{request['fee_currency']} {request['fee_amount']:,.2f}** "
            f"({request['fee_source']})\n\n"
            f"Shall I go ahead? Say **yes** to pay and submit it, or **no** to cancel.")


def _service_request_turn(session_id: str, message: str, trace: List[Dict],
                          sentiment_result: Dict) -> Optional[Dict]:
    """A name correction in progress, a new one, or a question about one.
    None when the message is none of these."""
    request = service_requests.in_dialog(session_id)

    if request is None:
        if service_requests.STATUS_RE.search(message):
            last = service_requests.latest(session_id)
            if last:
                return _finalize_direct(session_id, message, _request_status_reply(last), trace,
                                        sentiment_result)
        if not service_requests.START_RE.search(message) or service_requests.INFO_RE.search(message):
            return None
        question = _start_name_request(session_id, trace)
        if question is None:
            service_requests.start(session_id, service_requests.AWAITING_BOOKING)
            question = ("I can help you correct the name on your booking. First, please send your "
                        "booking reference and the surname on the booking, for example "
                        "\"booking KQDLW1, surname Wangui\".")
        return _finalize_direct(session_id, message, _apply_empathy(question, sentiment_result),
                                trace, sentiment_result)

    if service_requests.NO_RE.search(message):
        service_requests.update(request["id"], status=service_requests.CANCELLED)
        return _finalize_direct(session_id, message,
                                "No problem - I've cancelled the name correction. Nothing has been charged.",
                                trace, sentiment_result)

    if request["status"] == service_requests.AWAITING_BOOKING:
        return None            # the booking lookup (or another question) handles this message

    # A different booking reference part-way through means the passenger wants
    # to correct that booking instead (the request may have started from one
    # looked up earlier in the conversation). Go back to waiting for a booking
    # and let the lookup step check it; the same request then carries on.
    other = booking.find_reference(message)
    if other and other != (request.get("booking_reference") or "").upper():
        service_requests.update(
            request["id"], status=service_requests.AWAITING_BOOKING, booking_reference=None,
            current_name=None, scope=None, new_name=None, characters_changed=None,
            change_kind=None, fee_amount=None, fee_currency=None, fee_source=None)
        trace.append({"step": "thought",
                      "content": f"Passenger gave booking {other} for name correction "
                                 f"{service_requests.reference(request)}, not "
                                 f"{request.get('booking_reference')}; looking that booking up instead."})
        return None

    if request["status"] == service_requests.AWAITING_NEW_NAME:
        new_name = service_requests.parse_new_name(message, request["current_name"])
        if new_name is None:
            return _finalize_direct(session_id, message,
                                    f"Please send the name as it should appear on the booking, for "
                                    f"example \"{request['current_name']}\" with the spelling corrected, "
                                    f"or say \"cancel\".", trace, sentiment_result)
        if new_name.upper() == request["current_name"].upper():
            return _finalize_direct(session_id, message,
                                    "That is the same as the name already on the booking. What should "
                                    "it be changed to?", trace, sentiment_result)
        chunks = get_vector_store().similarity_search("name correction full name change fee", k=4)
        fees = service_requests.name_fees(chunks)
        trace.append({"step": "action", "tool": "retrieve_policy_documents",
                      "input": "name correction full name change fee",
                      "observation": [{"source": c.get("source"), "section": c.get("section")} for c in chunks]})
        if fees is None:
            return _finalize_direct(session_id, message,
                                    "I couldn't find the name-correction fees in the policy documents, so I "
                                    "can't price this safely. Please contact Kenya Airways customer service.",
                                    trace, sentiment_result)
        changed = service_requests.characters_changed(request["current_name"], new_name)
        quote = service_requests.price(fees, request["scope"] or "international", changed)
        request = service_requests.update(
            request["id"], new_name=new_name, characters_changed=changed, change_kind=quote["kind"],
            fee_amount=quote["amount"], fee_currency=quote["currency"], fee_source=fees["source"],
            status=service_requests.AWAITING_CONFIRMATION)
        trace.append({"step": "thought",
                      "content": f"{changed} character(s) differ, so this is a name {quote['kind']} on a "
                                 f"{request['scope']} booking: {quote['currency']} {quote['amount']:,.2f} "
                                 f"from {fees['source']}."})
        return _finalize_direct(session_id, message, _name_request_summary(request), trace,
                                sentiment_result)

    # Awaiting confirmation.
    if service_requests.YES_RE.search(message):
        description = service_requests.description(request)
        quote_id = payments.new_quote(session_id, request["fee_amount"], request["fee_currency"],
                                      description, stage="awaiting_method")
        service_requests.update(request["id"], quote_id=quote_id,
                                status=service_requests.AWAITING_PAYMENT)
        trace.append({"step": "thought",
                      "content": f"Passenger confirmed {service_requests.reference(request)}; quoting "
                                 f"{request['fee_currency']} {request['fee_amount']:,.2f} for payment."})
        reply = (f"Thank you. Request **{service_requests.reference(request)}** will be submitted to our "
                 f"team as soon as the fee is paid.\n\n"
                 + _ask_for_method_message(request["fee_amount"], request["fee_currency"]))
        return _finalize_direct(session_id, message, reply, trace, sentiment_result)
    return _finalize_direct(session_id, message,
                            "Please say **yes** to pay and submit the name correction, or **no** to cancel.",
                            trace, sentiment_result)


def _request_status_reply(request: Dict) -> str:
    ref = service_requests.reference(request)
    change = f"{request.get('current_name')} to {request.get('new_name')}"
    return {
        service_requests.SUBMITTED: (f"Request {ref} ({change}) is paid and with our team, who will update "
                                     f"the booking. Payment reference {request.get('payment_reference')}."),
        service_requests.COMPLETED: f"Request {ref} is complete: the booking now shows {request.get('new_name')}.",
        service_requests.AWAITING_PAYMENT: (f"Request {ref} ({change}) is waiting for the fee to be paid. "
                                            f"Once the payment is confirmed it goes to our team."),
        service_requests.CANCELLED: f"Request {ref} was cancelled and nothing was charged for it.",
    }.get(request["status"], f"Request {ref} is not finished yet.")


def _booking_turn(session_id: str, message: str, trace: List[Dict],
                  sentiment_result: Dict) -> Optional[Dict]:
    """Looks up a booking when the message gives a booking reference. None
    when it gives none."""
    reference = booking.find_reference(message)
    if not reference:
        return None
    surname = booking.find_surname(message)
    if not surname:
        trace.append({"step": "thought",
                       "content": "Booking reference given without a surname; both are required."})
        return _finalize_direct(session_id, message, _apply_empathy(
            f"To look up booking {reference} I also need the surname on the booking. Please send "
            f"both together, for example \"booking {reference}, surname Kanja\".", sentiment_result),
            trace, sentiment_result)
    result = booking.lookup(session_id, reference, surname)
    # The surname is not copied into the trace, which staff can see.
    # Passenger names are kept out of the trace, which staff can see.
    trace.append({"step": "action", "tool": "lookup_booking", "input": reference,
                  "observation": {"source": result["source"], "available": result["available"],
                                  "reason": result["reason"], "segments": result["segments"]}})
    reply = booking.describe(result, reference)
    if result["available"]:
        first = result["segments"][0]
        memory.remember(session_id, booking=json.dumps({"reference": reference,
                                                         "segments": result["segments"],
                                                         "passengers": result["passengers"]}),
                        flight_number=first.get("flight_number"))
        # A name correction that was waiting for the booking carries on.
        waiting = service_requests.in_dialog(session_id)
        if waiting and waiting["status"] == service_requests.AWAITING_BOOKING:
            follow_up = _start_name_request(session_id, trace)
            if follow_up:
                reply = reply.rsplit("\n\nYou can now ask", 1)[0] + "\n\n" + follow_up
    return _finalize_direct(session_id, message, _apply_empathy(reply, sentiment_result),
                            trace, sentiment_result)


def _handover_turn(session_id: str, message: str, trace: List[Dict],
                   sentiment_result: Dict) -> Optional[Dict]:
    """Handles a request for a person, and messages sent while a person is
    handling the conversation. None when neither applies."""
    case = handover.active(session_id)
    if case:
        ref = handover.reference(case)
        if handover.CANCEL_RE.search(message):
            handover.withdraw(session_id)
            trace.append({"step": "thought",
                          "content": f"Passenger withdrew handover case {ref}."})
            return _finalize_direct(
                session_id, message,
                f"No problem - I've closed case {ref} and I'm back with you. "
                "What can I help you with?", trace, sentiment_result)
        # A member of staff owns the conversation now. Answering from policy
        # as well would give the passenger two voices that may disagree.
        trace.append({"step": "thought",
                      "content": (f"Handover case {ref} is {case['status']}; adding the "
                                  f"message to the case instead of answering it.")})
        reply = (f"Thanks - I've added that to your case {ref}. A member of our support team "
                 "will reply here. If you'd rather carry on with me, say \"back to assistant\".")
        return _finalize_direct(session_id, message, reply, trace, sentiment_result,
                                handover_case=handover.status_for_passenger(session_id))

    if not handover.is_request(message):
        return None

    if not config.HANDOVER_ENABLED:
        trace.append({"step": "thought",
                      "content": "Passenger asked for a person; handover is not enabled, so "
                                 "giving the airline's contact route instead of promising one."})
        reply = ("I'm not able to connect you to a person from this chat. You can reach a "
                 f"member of staff through {config.HANDOVER_FALLBACK_CONTACT}.\n\n"
                 "I'm happy to keep helping here in the meantime.")
        return _finalize_direct(session_id, message, reply, trace, sentiment_result)

    case = handover.open_case(session_id, reason=message,
                              frustration_score=sentiment_result.get("frustration_score"))
    ref = handover.reference(case)
    trace.append({"step": "action", "tool": "open_handover_case", "input": message,
                  "observation": {"reference": ref, "status": case["status"],
                                  "forwarded": case.get("forwarded")}})
    reply = (f"I've passed this conversation to our support team (case {ref}). A member of "
             "staff will reply here in this chat, so please keep this window open.\n\n"
             "If you'd rather carry on with me, say \"back to assistant\".")
    return _finalize_direct(session_id, message, reply, trace, sentiment_result,
                            handover_case=handover.status_for_passenger(session_id))


# Words that say what kind of thing is asked ("fee", "allowed") but not about
# what. A question whose subject words are all of these names no subject.
_VAGUE_WORDS = frozenset({
    "fee", "cost", "charge", "price", "rate", "amount", "allowed", "allow", "permitted", "permit",
    "possible", "ok", "okay", "fine", "rule", "policy", "claim", "entitled", "owe", "owed",
    "bring", "take", "carry",
})
_VAGUE_FEE_RE = re.compile(r"\b(fees?|costs?|charges?|prices?|rates?|amount|how\s+much|pay)\b", re.I)
_VAGUE_ALLOWED_RE = re.compile(r"\b(allowed|allow|permitted|permit|possible|ok|okay|fine|bring|take|carry)\b", re.I)
_VAGUE_CLAIM_RE = re.compile(r"\b(claim|entitled|owed?|compensat\w*)\b", re.I)
_QUESTION_RE = re.compile(r"\?\s*$|^(how|what|which|is|are|can|could|do|does|will|may|am)\b", re.I)


def _vague_question(message: str) -> Optional[str]:
    """A clarifying question when the message asks something without saying
    about what; None when it names a subject."""
    if not _QUESTION_RE.search(message.strip()) or len(message.split()) > 12:
        return None
    topical = retrieval.topical_tokens(message)
    if any(t not in _VAGUE_WORDS for t in topical):
        return None
    if _VAGUE_CLAIM_RE.search(message):
        return ("What happened? For example, a checked bag that has not arrived, a delayed flight, "
                "or a cancelled trip you want a refund for. Tell me and I'll check what the policy says.")
    if _VAGUE_FEE_RE.search(message):
        return ("Which fee do you mean? For example, an extra or overweight bag, sporting equipment "
                "such as golf clubs, changing or cancelling a booking, or correcting the name on a "
                "booking.")
    if _VAGUE_ALLOWED_RE.search(message):
        return ("What would you like to bring or do? For example, a second cabin bag, golf clubs "
                "or other sporting equipment, a stroller, or a wheelchair. Tell me and I'll check "
                "the rules.")
    if topical:
        return ("Could you tell me a little more about what you need? I can help with baggage, "
                "check-in, delays, refunds and changes, and paying fees.")
    return None


def _small_talk_reply(message: str) -> Optional[str]:
    if _GREETING_RE.match(message):
        return GREETING_REPLY
    if _THANKS_RE.match(message):
        return THANKS_REPLY
    if _BYE_RE.match(message):
        return BYE_REPLY
    if _HELP_RE.search(message):
        return CAPABILITY_REPLY
    return None


# How many policy sections the agent retrieves for a question.
RETRIEVAL_K = 4


def retrieve(store, query: str, k: int = RETRIEVAL_K) -> List[Dict]:
    """The policy sections for a question. Every tool that measures the agent
    calls this too, so evaluation sees exactly what passengers get.

    Searching each part of a multi-part question separately was tried and
    rejected: retrieval already finds every expected document for 29 of the
    30 evaluation questions at k=4, and per-part search left answer relevance
    unchanged (0.633) while lowering context relevance (0.405 to 0.400). The
    gain came from composing each part separately (composer.split_question).
    """
    return store.similarity_search(query, k=k)


def _serialize_context(chunks: List[Dict]) -> str:
    blocks = []
    for c in chunks:
        source = (c.get("source") or "").replace(".txt", "").replace("_", " ").title()
        label = f"{source} | {c.get('section', '')}".strip(" |")
        blocks.append(f"[{label}]\n{c['text']}")
    return "\n---\n".join(blocks)


def _extract_fee_from_context(chunks: List[Dict], message: str) -> Optional[Tuple[float, str]]:
    """The fee the policy states for this passenger's situation, with the
    currency it is published in, or None.

    This is the only source of a payment amount. An earlier version took the
    amount from the passenger's own message ("pay Ksh 1") and only recognised
    shilling figures, so it could charge a typed amount and missed every fee
    Kenya Airways publishes in dollars.
    """
    grounded = composer.compose_answer(message, chunks, max_sentences=2)
    return _payable_fee(grounded, chunks, message)


def _finalize_direct(session_id: str, message: str, answer: str, trace: List[Dict],
                      sentiment_result: Dict, model_name: str = "rule-based",
                      payment: Optional[Dict] = None,
                      handover_case: Optional[Dict] = None) -> Dict:
    """Logs and returns a turn that was answered without document grounding
    (small talk, a schedule request, a payment step, or an out-of-scope
    question)."""
    trace.append({"step": "final_answer", "model": model_name, "content": answer})
    database.log_message(session_id, "user", message,
                          sentiment_label=sentiment_result["label"],
                          frustration_score=sentiment_result["frustration_score"])
    message_id = database.log_message(session_id, "assistant", answer)
    return {
        "answer": answer, "model_used": model_name,
        "sentiment": sentiment_result, "sources": [], "trace": trace,
        "rag_metrics": {}, "payment": payment, "handover": handover_case,
        "message_id": message_id,
    }


def run_agent_turn(session_id: str, message: str) -> Dict:
    """One passenger turn. The reply text is unchanged by the cards; they are
    a structured copy of parts of it that the chat can draw as cards."""
    result = _run_agent_turn(session_id, message)
    result["cards"] = _cards_for_turn(session_id, result)
    return result


# ------------------------------------------------------------------ answer cards

def _cards_for_turn(session_id: str, result: Dict) -> List[Dict]:
    """Structured versions of a flight status, a found booking and a name-change
    summary in this turn's reply. Each card names the exact passage of the
    answer it stands for ("replaces"), so the chat can show the card in its
    place; the stored answer, the trace and the staff transcript keep the text.
    A card is only produced when that passage is really in the answer."""
    answer = result.get("answer") or ""
    cards: List[Dict] = []
    for step in result.get("trace") or []:
        tool = step.get("tool")
        obs = step.get("observation")
        if tool == "get_flight_status" and isinstance(obs, dict) and obs.get("available"):
            text = aviationstack.describe(obs)
            if text in answer:
                cards.append({
                    "type": "flight", "replaces": text,
                    "flight_number": obs.get("flight_number"), "route": obs.get("route"),
                    "status": obs.get("status"), "gate": obs.get("gate"),
                    "delay_minutes": obs.get("delay_minutes"),
                    "estimated_departure_utc": obs.get("estimated_departure_utc"),
                    "retrieved_at_utc": obs.get("retrieved_at_utc"),
                    "is_live": bool(obs.get("is_live")),
                    "source_label": (aviationstack.LIVE_SOURCES.get(obs.get("source"), "Live data")
                                     if obs.get("is_live") else "Demonstration data, not live"),
                })
        elif tool == "lookup_booking" and isinstance(obs, dict) and obs.get("available"):
            trip = booking.remembered_booking(session_id)
            if not trip.get("reference"):
                continue
            is_live = obs.get("source") == booking.SOURCE_LIVE
            full = booking.describe({"available": True, "segments": trip["segments"],
                                     "passengers": trip.get("passengers") or [], "is_live": is_live},
                                    trip["reference"])
            block = full.rsplit("\n\nYou can now ask", 1)[0]
            if block in answer:
                cards.append({"type": "booking", "replaces": block, "reference": trip["reference"],
                              "passengers": trip.get("passengers") or [],
                              "segments": trip["segments"], "is_live": is_live})
    request = service_requests.latest(session_id)
    if request and request.get("status") == service_requests.AWAITING_CONFIRMATION \
            and request.get("fee_amount") is not None:
        summary = _name_request_summary(request)
        if summary in answer:
            cards.append({
                "type": "name_change", "replaces": summary,
                "reference": service_requests.reference(request),
                "booking_reference": request["booking_reference"], "scope": request["scope"],
                "current_name": request["current_name"], "new_name": request["new_name"],
                "characters_changed": request["characters_changed"],
                "change_kind": request["change_kind"],
                "fee_currency": request["fee_currency"], "fee_amount": request["fee_amount"],
                "fee_source": request["fee_source"],
            })
    return cards


def _run_agent_turn(session_id: str, message: str) -> Dict:
    message = (message or "").strip()
    trace: List[Dict] = []

    if not message:
        return {
            "answer": "Please type a question and I'll help you out.",
            "model_used": "rule-based", "sentiment": {"label": "calm", "confidence": 1.0,
                                                       "frustration_score": 0.0},
            "sources": [], "trace": [], "rag_metrics": {}, "payment": None, "handover": None,
        }

    # --- Thought 1: classify passenger tone (custom-trained ML model) ---
    sentiment_result = sentiment.classify_frustration(message)
    trace.append({
        "step": "thought",
        "content": (f"Classify passenger tone before responding. Detected: "
                    f"{sentiment_result['label']} (score={sentiment_result['frustration_score']})"),
    })

    # --- Thought 2: a request for a person, or a person already on the case ---
    handed_over = _handover_turn(session_id, message, trace, sentiment_result)
    if handed_over:
        return handed_over

    # --- A name correction in progress, or a new one ---
    requested = _service_request_turn(session_id, message, trace, sentiment_result)
    if requested:
        return requested

    # --- Thought 2b: continue an in-progress payment conversation ---
    pending = database.get_pending_payment(session_id)
    if pending:
        resumed = _handle_pending_payment(session_id, message, pending, trace, sentiment_result)
        if resumed:
            return resumed

    # --- Thought 3: conversational intent shortcut ---
    small_talk = _small_talk_reply(message)
    if small_talk:
        trace.append({"step": "thought",
                       "content": "Conversational intent detected - no document retrieval needed."})
        return _finalize_direct(session_id, message, small_talk, trace, sentiment_result)

    # --- Action: the passenger's own booking, from a reference and surname ---
    booked = _booking_turn(session_id, message, trace, sentiment_result)
    if booked:
        return booked

    # --- Thought 3: a departure/arrival time needs live telemetry, and that
    # tool is keyed on a flight number. Ask for it instead of falling back to
    # an unrelated policy section. ---
    flight_number = _detect_flight_number(message)
    if flight_number is None:
        flight_number = memory.resolve_flight(session_id, message)
        if flight_number:
            trace.append({"step": "thought",
                          "content": (f"\"That flight\" refers to {flight_number}, named "
                                      f"earlier in the conversation.")})
    if _SCHEDULE_RE.search(message) and not flight_number:
        trace.append({
            "step": "thought",
            "content": ("Flight schedule intent detected, but no flight number was supplied. "
                        "The telemetry tool requires one, so request it rather than answering "
                        "from policy documents."),
        })
        return _finalize_direct(session_id, message,
                                 _apply_empathy(_schedule_request_reply(message), sentiment_result),
                                 trace, sentiment_result)

    # --- Action: hybrid semantic + lexical retrieval over policy documents ---
    # The emotional framing a passenger wraps around a question is signal for
    # the frustration classifier, not for retrieval, so search on the request.
    #
    # A follow-up ("what about Business class?") is first rewritten into the
    # complete question it stands for, using the previous answered question.
    search_text = message
    # A Swahili question is searched in English through a small glossary and
    # answered in English, with a line in Swahili saying so (app/swahili.py).
    swahili_english = None
    if swahili.is_swahili(message):
        swahili_english = swahili.to_english(message)
        if not swahili_english:
            trace.append({"step": "thought",
                          "content": ("The question is in Swahili and the glossary recognises too few of "
                                      "its words to search the English policy. Declining in Swahili and "
                                      "English rather than guessing.")})
            return _finalize_direct(session_id, message, swahili.DECLINE, trace, sentiment_result)
        search_text = swahili_english
        trace.append({"step": "thought",
                      "content": (f"The question is in Swahili. The policy documents are in English, so "
                                  f"its travel terms were looked up in the glossary: searching for "
                                  f"\"{swahili_english}\" and answering in English.")})
    resolved = None if swahili_english else memory.resolve_query(session_id, message)
    if resolved:
        search_text = resolved
        trace.append({"step": "thought",
                       "content": (f"Follow-up to the previous question. Searching for "
                                   f"\"{resolved}\".")})
    # A baggage or check-in question that names no cabin or route is answered
    # for the trip the passenger has looked up, if any.
    enriched = booking.enrich(search_text, booking.remembered(session_id))
    if enriched:
        search_text = enriched
        trace.append({"step": "thought",
                       "content": f"Answering for the booked trip. Searching for \"{enriched}\"."})
    # --- Thought: a question with no subject ("How much is the fee?", "Is it
    # allowed?") used to be answered with whichever section matched a generic
    # word. Read it against the previous question if there is one; otherwise
    # ask what the passenger means rather than guess. ---
    if not resolved and not enriched and not flight_number:
        clarify = _vague_question(message)
        if clarify:
            previous = memory.recall(session_id).get("last_query")
            if previous:
                # One sentence, so it is not split back into two questions.
                search_text = f"{previous.rstrip('?.! ')}, {message[0].lower()}{message[1:]}"
                trace.append({"step": "thought",
                              "content": (f"The question names no subject, so it is read as being about "
                                          f"the previous one. Searching for \"{search_text}\".")})
            else:
                trace.append({"step": "thought",
                              "content": ("The question names no subject and there is no earlier question "
                                          "to read it against. Asking what is meant instead of guessing.")})
                return _finalize_direct(session_id, message,
                                        _apply_empathy(clarify, sentiment_result),
                                        trace, sentiment_result)
    query = retrieval.focus_query(search_text)
    if query != search_text:
        trace.append({"step": "thought",
                       "content": f"Isolated the actual request from the message: \"{query}\""})
    store = get_vector_store()
    retrieved = retrieve(store, query)
    trace.append({
        "step": "action",
        "tool": "retrieve_policy_documents",
        "input": query,
        "observation": [
            {"source": r.get("source"), "section": r.get("section"),
             "provenance": r.get("provenance", "undeclared"),
             "score": r.get("score"), "excerpt": r["text"][:160]}
            for r in retrieved
        ],
    })

    # --- Observation: is the retrieved policy text actually on topic? ---
    # The isolated request clause can be too short to show its subject ("What
    # can I claim?"), so the whole message is consulted as well. Filler and
    # place words are ignored either way, so venting cannot ground an answer.
    grounded = _is_grounded_in_context(query, retrieved) or (
        query != search_text and _is_grounded_in_context(search_text, retrieved))
    if not grounded and not flight_number \
            and not _PAYMENT_TOPIC_RE.search(message):
        trace.append({
            "step": "thought",
            "content": ("Retrieved sections do not overlap the question's subject matter. "
                        "Declining to answer rather than risk an ungrounded response."),
        })
        return _finalize_direct(session_id, message,
                                 swahili.DECLINE if swahili_english else
                                 _apply_empathy(OUT_OF_SCOPE_REPLY, sentiment_result),
                                 trace, sentiment_result)

    tool_observations = []

    # --- Action: live flight telemetry tool (AviationStack) ---
    if flight_number:
        memory.remember(session_id, flight_number=flight_number)
        flight_status = kq_flights.get_flight_status(flight_number)
        trace.append({"step": "action", "tool": "get_flight_status",
                       "input": flight_number, "observation": flight_status})
        tool_observations.append(aviationstack.describe(flight_status))
        if _is_flight_status_only(search_text):
            retrieved = []
            trace.append({"step": "thought",
                           "content": ("The question only asks about the flight's status, so it is "
                                       "answered from the flight tool alone, without policy text.")})

    # --- Action: payment tools (M-Pesa STK push, or a card checkout) ---
    payment_result = None
    payment_prompt_text = None
    if _TRANSACTIONAL_RE.search(message):
        phone_match = _PHONE_RE.search(message)
        fee = _extract_fee_from_context(retrieved, query)
        description = "KQ-Propel ancillary fee settlement"

        if fee is None:
            tool_observations.append(
                "Payment requested but the applicable fee could not be determined from "
                "policy. Ask the passenger which fee they want to pay."
            )
            trace.append({"step": "thought",
                           "content": "Payment intent detected but no grounded amount available."})
        else:
            amount, currency = fee
            typed = _AMOUNT_RE.search(message)
            if typed and float(typed.group(1).replace(",", "")) != amount:
                trace.append({"step": "thought",
                               "content": (f"Passenger named an amount ({typed.group(0)}) that "
                                           f"differs from the policy fee of {currency} "
                                           f"{amount:,.2f}; only the policy fee may be charged.")})
                payment_prompt_text = (f"I can only take the fee the policy states, which is "
                                       f"**{currency} {amount:,.2f}**, not the amount in your "
                                       f"message.")
            quote_id = payments.new_quote(session_id, amount, currency, description,
                                          stage="awaiting_method")
            if phone_match:
                phone = _normalize_phone(phone_match)
                kes_amount, conversion_note = _to_kes(amount, currency)
                payment_result = _execute_payment(session_id, phone, kes_amount, description,
                                                  trace, quote_id=quote_id)
                text = (_payment_sent_message(kes_amount, phone, payment_result)
                        if payment_result.get("success") else
                        payment_result.get("message") if payment_result.get("rate_limited") else
                        "I wasn't able to send the M-Pesa prompt just now. Nothing has been "
                        "charged - please try again in a moment.")
                if conversion_note and payment_result.get("success") \
                        and not payment_result.get("duplicate"):
                    text = conversion_note + "\n\n" + text
                payment_prompt_text = "\n\n".join(t for t in (payment_prompt_text, text) if t)
            elif _CARD_RE.search(message):
                payment_result = _execute_card_payment(session_id, amount, currency, description,
                                                       trace, quote_id=quote_id)
                text = (_card_checkout_message(amount, currency, payment_result)
                        if payment_result.get("success") else
                        "I couldn't create the card checkout just now. Nothing has been charged "
                        "- please try again in a moment.")
                payment_prompt_text = "\n\n".join(t for t in (payment_prompt_text, text) if t)
            else:
                trace.append({"step": "thought",
                               "content": (f"Payment intent detected for {currency} {amount:,.2f}; "
                                           f"awaiting the passenger's choice of rail.")})
                reply = _ask_for_method_message(amount, currency)
                if payment_prompt_text:
                    reply = payment_prompt_text + "\n\n" + reply
                return _finalize_direct(session_id, message,
                                         _apply_empathy(reply, sentiment_result),
                                         trace, sentiment_result)
    elif _PAYMENT_TOPIC_RE.search(message):
        tool_observations.append(
            "The passenger is asking about payment options. Explain them, and mention that "
            "I can either generate a secure card checkout link or send an M-Pesa STK push "
            "to a Kenyan number, directly in this chat, if they say they want to pay."
        )

    # --- Compose the final grounded answer ---
    context_text = _serialize_context(retrieved)
    tool_context = "\n".join(tool_observations)
    user_prompt = f"CONTEXT:\n{context_text}\n\nTOOL OBSERVATIONS:\n{tool_context}\n\nQUESTION:\n{query}"

    # Sentiment changes tone, never content. A note about the passenger's
    # frustration used to be appended here; the composer read its words
    # ("inconvenience", "escalation", "agent") as part of the question, so a
    # frustrated passenger was given different policy text from a calm one for
    # 6 of 30 evaluation questions and for everyday questions such as the
    # name-correction fee (tools/test_sentiment_fairness.py). Empathy
    # is added by _apply_empathy() after generation, outside the policy answer.

    generated = generate_answer(SYSTEM_PROMPT, user_prompt)
    answer, model_name = generated["answer"], generated["model"]
    if generated["fallback"]:
        trace.append({"step": "thought",
                       "content": (f"{generated['fallback']}. Answered from the offline "
                                   f"composer instead, which only quotes the policy text.")})

    if flight_number and tool_observations:
        # With no policy text the composer can only repeat the status, so the
        # status is the whole answer.
        answer = (tool_observations[0] if not retrieved
                  else tool_observations[0] + "\n\n" + answer)

    trip_note = booking.allowance_note(message, booking.remembered(session_id))
    if trip_note:
        trace.append({"step": "observation", "content": f"From the booking record: {trip_note}"})
        answer = trip_note + "\n\n" + answer

    if payment_prompt_text:
        answer += "\n\n" + payment_prompt_text
    elif payment_result and payment_result.get("message"):
        answer += "\n\n" + payment_result["message"]
    else:
        # A grounded, payable fee is an opportunity to settle it there and
        # then, which is the localized transaction-execution capability the
        # proposal is built around.
        fee = _payable_fee(answer, retrieved, message)
        if fee is not None:
            amount, currency = fee
            # The fee is held in the currency the policy publishes it in. A
            # shilling figure is derived only to show what the M-Pesa option
            # would cost, and the conversion is disclosed; a guest who pays by
            # card never encounters this assistant's exchange-rate assumption
            # at all.
            kes_amount, conversion_note = _to_kes(amount, currency)
            payments.new_quote(session_id, amount, currency,
                               "KQ-Propel ancillary fee settlement", stage="offered")
            answer += _offer_payment_message(amount, currency, kes_amount)
            if conversion_note:
                answer += "\n\n" + conversion_note
            trace.append({"step": "thought",
                           "content": (
                               f"Grounded payable fee of {currency} {amount:,.2f} identified - "
                               f"offering both settlement rails."
                               if conversion_note is None else
                               f"Grounded payable fee of USD {amount:,.0f} identified. Offered "
                               f"by card in USD (no conversion) and by M-Pesa, which settles "
                               f"only in shillings, at the declared prototype rate of "
                               f"{config.USD_TO_KES_RATE:,.2f} KES/USD giving "
                               f"Ksh {kes_amount:,.0f}; the rate is disclosed in the reply.")})

    # Empathy framing wraps the completed answer so the acknowledgement always
    # comes first and the escalation offer always comes last.
    answer = _apply_empathy(answer, sentiment_result)

    # Disclose prototype-authored evidence regardless of which generator
    # produced the answer. The offline composer adds this itself; a live LLM
    # would not, so the check is repeated here against the cited sections.
    if composer.SYNTHETIC_SOURCE_NOTE not in answer:
        cited_synthetic = [
            r for r in retrieved
            if r.get("provenance") == "synthetic" and (r.get("section") or "") in answer
        ]
        if cited_synthetic:
            answer += "\n\n" + composer.SYNTHETIC_SOURCE_NOTE

    if swahili_english:
        answer = swahili.ANSWER_NOTE.format(english=swahili_english) + "\n\n" + answer

    trace.append({"step": "final_answer", "model": model_name, "content": answer})
    memory.remember(session_id, query=query)

    # Relevance is judged against the English question the answer was built for.
    metrics = evaluation.evaluate_response(swahili_english or message, answer, retrieved)
    sources = []
    for r in retrieved:
        label = composer._section_label(r)
        if label not in sources:
            sources.append(label)

    database.log_message(session_id, "user", message,
                          sentiment_label=sentiment_result["label"],
                          frustration_score=sentiment_result["frustration_score"])
    message_id = database.log_message(session_id, "assistant", answer, sources=", ".join(sources))
    database.log_rag_evaluation(query_id=str(uuid.uuid4()), query=message,
                                 context_relevance=metrics["context_relevance"],
                                 groundedness=metrics["groundedness"],
                                 answer_relevance=metrics["answer_relevance"], model=model_name,
                                 session_id=session_id, message_id=message_id)

    return {
        "answer": answer,
        "model_used": model_name,
        "handover": None,
        "sentiment": sentiment_result,
        "sources": sources,
        "trace": trace,
        "rag_metrics": metrics,
        "payment": payment_result,
        "message_id": message_id,
    }
