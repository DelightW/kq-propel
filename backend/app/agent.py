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
from typing import Dict, List, Optional, Tuple

from app import (aviationstack, composer, config, daraja, database, evaluation,
                  retrieval, sentiment)
from app.llm import get_primary_llm
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
_PAYMENT_TOPIC_RE = re.compile(r"\b(pay|payment|m-?pesa|mpesa|stk|till|settle)\b", re.IGNORECASE)

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


def _is_grounded_in_context(message: str, chunks: List[Dict]) -> bool:
    """True when the retrieved policy text actually shares subject matter with
    the question. Retrieval always returns its best candidates, so without this
    check an off-topic question is answered from whichever section ranked
    highest - which is how a departure-time question ended up being answered
    with refund processing timelines."""
    query_tokens = set(retrieval.tokenize(message))
    if not query_tokens:
        return False
    for chunk in chunks[:2]:
        chunk_tokens = set(retrieval.tokenize(
            chunk.get("search_text") or chunk.get("text") or ""))
        if len(query_tokens & chunk_tokens) / len(query_tokens) >= 0.3:
            return True
    return False


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


def _execute_payment(session_id: str, phone: str, amount: float, description: str,
                      trace: List[Dict]) -> Dict:
    """Runs the STK push, records it in the audit trail, and clears any
    outstanding payment offer for the session."""
    reference = f"KQPROPEL-{uuid.uuid4().hex[:8].upper()}"
    result = daraja.initiate_stk_push(
        phone_number=phone, amount=amount, reference=reference, description=description,
    )
    trace.append({"step": "action", "tool": "initiate_mpesa_stk_push",
                   "input": {"phone": phone, "amount": amount, "reference": reference},
                   "observation": result})
    database.log_transaction(
        session_id=session_id,
        checkout_request_id=result.get("checkout_request_id", ""),
        phone_number=phone, amount=amount, reference=reference,
        description=description,
        status="initiated" if result.get("success") else "failed",
    )
    database.clear_pending_payment(session_id)
    result.setdefault("amount", amount)
    result.setdefault("phone_number", phone)
    result.setdefault("reference", reference)
    return result


def _payment_sent_message(amount: float, phone: str) -> str:
    return (
        f"Done - I've sent an M-Pesa payment prompt for **Ksh {amount:,.0f}** to {phone}.\n\n"
        "Check your phone: a payment request should appear within a few seconds. Enter your "
        "M-Pesa PIN to authorise it, and you'll get a confirmation SMS from Safaricom.\n\n"
        "If the prompt doesn't arrive, tell me and I'll send it again."
    )


def _ask_for_phone_message(amount: float) -> str:
    return (
        f"Happy to help you settle that. The amount due is **Ksh {amount:,.0f}**.\n\n"
        "What's the M-Pesa number I should send the payment prompt to? You can give it as "
        "07XX XXX XXX or 2547XX XXX XXX."
    )


def _offer_payment_message(amount: float) -> str:
    return (
        f"\n\nWould you like me to send an M-Pesa payment prompt for Ksh {amount:,.0f}? "
        "Just reply with your M-Pesa number (for example 0712345678) and I'll push it to "
        "your phone straight away."
    )


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
    description = pending.get("description") or "KQ-Propel ancillary fee settlement"

    if _DECLINE_RE.match(message):
        database.clear_pending_payment(session_id)
        trace.append({"step": "thought", "content": "Passenger declined the payment offer."})
        return _finalize_direct(
            session_id, message,
            "No problem - I won't send the payment prompt. Let me know if you'd like to "
            "pay later, or if there's anything else I can help you with.",
            trace, sentiment_result)

    phone_match = _PHONE_RE.search(message)
    if phone_match:
        phone = _normalize_phone(phone_match)
        trace.append({"step": "thought",
                       "content": (f"M-Pesa number supplied for the outstanding Ksh {amount:,.0f} "
                                   f"payment. Initiating STK push.")})
        result = _execute_payment(session_id, phone, amount, description, trace)
        answer = _payment_sent_message(amount, phone) if result.get("success") else (
            "I wasn't able to send the M-Pesa prompt just now. Please try again in a moment, "
            "or pay at the airport counter.")
        return _finalize_direct(session_id, message, _apply_empathy(answer, sentiment_result),
                                 trace, sentiment_result, payment=result)

    if _AFFIRM_RE.match(message) or _TRANSACTIONAL_RE.search(message):
        database.set_pending_payment(session_id, amount, description, "awaiting_phone")
        trace.append({"step": "thought",
                       "content": "Payment confirmed; awaiting the passenger's M-Pesa number."})
        return _finalize_direct(session_id, message,
                                 _apply_empathy(_ask_for_phone_message(amount), sentiment_result),
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
    if sentiment_result["label"] != "frustrated" or sentiment_result["frustration_score"] < 0.55:
        return answer
    return (
        "I'm really sorry for the trouble - I understand how frustrating this is, "
        "and I'll help you sort it out right away.\n\n" + answer +
        "\n\nIf this doesn't fully resolve things, I can escalate you to a human "
        "support agent straight away - just say \"escalate\"."
    )


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


def _serialize_context(chunks: List[Dict]) -> str:
    blocks = []
    for c in chunks:
        source = (c.get("source") or "").replace(".txt", "").replace("_", " ").title()
        label = f"{source} | {c.get('section', '')}".strip(" |")
        blocks.append(f"[{label}]\n{c['text']}")
    return "\n---\n".join(blocks)


def _extract_fee_from_context(chunks: List[Dict], message: str) -> Optional[float]:
    """Finds the fee amount the policy actually specifies for this passenger's
    situation, so a payment is never initiated for an invented amount."""
    grounded = composer.compose_answer(message, chunks, max_sentences=2)
    amounts = re.findall(r"Ksh\s?([\d,]+)", grounded)
    if amounts:
        return float(amounts[0].replace(",", ""))
    return None


def _finalize_direct(session_id: str, message: str, answer: str, trace: List[Dict],
                      sentiment_result: Dict, model_name: str = "rule-based",
                      payment: Optional[Dict] = None) -> Dict:
    """Logs and returns a turn that was answered without document grounding
    (small talk, a schedule request, a payment step, or an out-of-scope
    question)."""
    trace.append({"step": "final_answer", "model": model_name, "content": answer})
    database.log_message(session_id, "user", message,
                          sentiment_label=sentiment_result["label"],
                          frustration_score=sentiment_result["frustration_score"])
    database.log_message(session_id, "assistant", answer)
    return {
        "answer": answer, "model_used": model_name,
        "sentiment": sentiment_result, "sources": [], "trace": trace,
        "rag_metrics": {}, "payment": payment,
    }


def run_agent_turn(session_id: str, message: str) -> Dict:
    message = (message or "").strip()
    trace: List[Dict] = []

    if not message:
        return {
            "answer": "Please type a question and I'll help you out.",
            "model_used": "rule-based", "sentiment": {"label": "calm", "confidence": 1.0,
                                                       "frustration_score": 0.0},
            "sources": [], "trace": [], "rag_metrics": {}, "payment": None,
        }

    # --- Thought 1: classify passenger tone (custom-trained ML model) ---
    sentiment_result = sentiment.classify_frustration(message)
    trace.append({
        "step": "thought",
        "content": (f"Classify passenger tone before responding. Detected: "
                    f"{sentiment_result['label']} (score={sentiment_result['frustration_score']})"),
    })

    # --- Thought 2: continue an in-progress payment conversation ---
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

    # --- Thought 3: a departure/arrival time needs live telemetry, and that
    # tool is keyed on a flight number. Ask for it instead of falling back to
    # an unrelated policy section. ---
    flight_number = _detect_flight_number(message)
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
    query = retrieval.focus_query(message)
    if query != message:
        trace.append({"step": "thought",
                       "content": f"Isolated the actual request from the message: \"{query}\""})
    store = get_vector_store()
    retrieved = store.similarity_search(query, k=4)
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
    if not _is_grounded_in_context(query, retrieved) and not flight_number \
            and not _PAYMENT_TOPIC_RE.search(message):
        trace.append({
            "step": "thought",
            "content": ("Retrieved sections do not overlap the question's subject matter. "
                        "Declining to answer rather than risk an ungrounded response."),
        })
        return _finalize_direct(session_id, message,
                                 _apply_empathy(OUT_OF_SCOPE_REPLY, sentiment_result),
                                 trace, sentiment_result)

    tool_observations = []

    # --- Action: live flight telemetry tool (AviationStack) ---
    if flight_number:
        flight_status = aviationstack.get_flight_status(flight_number)
        trace.append({"step": "action", "tool": "get_flight_status",
                       "input": flight_number, "observation": flight_status})
        tool_observations.append(
            f"Live status for {flight_number}: {flight_status.get('status')}, "
            f"route {flight_status.get('route', 'n/a')}, gate {flight_status.get('gate', 'n/a')}, "
            f"delay {flight_status.get('delay_minutes', 0)} minutes."
        )

    # --- Action: payment tool (Safaricom Daraja STK push) ---
    payment_result = None
    payment_prompt_text = None
    if _TRANSACTIONAL_RE.search(message):
        phone_match = _PHONE_RE.search(message)
        amount_match = _AMOUNT_RE.search(message)
        if amount_match:
            amount = float(amount_match.group(1).replace(",", ""))
        else:
            amount = _extract_fee_from_context(retrieved, query)

        if amount is None:
            tool_observations.append(
                "Payment requested but the applicable fee could not be determined from "
                "policy. Ask the passenger to confirm the amount."
            )
            trace.append({"step": "thought",
                           "content": "Payment intent detected but no grounded amount available."})
        elif not phone_match:
            database.set_pending_payment(session_id, amount,
                                          "KQ-Propel ancillary fee settlement", "awaiting_phone")
            trace.append({"step": "thought",
                           "content": f"Payment intent detected for Ksh {amount:,.0f}; awaiting phone number."})
            return _finalize_direct(session_id, message,
                                     _apply_empathy(_ask_for_phone_message(amount), sentiment_result),
                                     trace, sentiment_result)
        else:
            phone = _normalize_phone(phone_match)
            payment_result = _execute_payment(
                session_id, phone, amount, "KQ-Propel ancillary fee settlement", trace)
            if payment_result.get("success"):
                payment_prompt_text = _payment_sent_message(amount, phone)
            tool_observations.append(f"Payment tool result: {payment_result.get('message')}")
    elif _PAYMENT_TOPIC_RE.search(message):
        tool_observations.append(
            "The passenger is asking about payment options. Explain them, and mention that "
            "I can send an M-Pesa STK push directly in this chat if they say they want to pay."
        )

    # --- Compose the final grounded answer ---
    context_text = _serialize_context(retrieved)
    tool_context = "\n".join(tool_observations)
    user_prompt = f"CONTEXT:\n{context_text}\n\nTOOL OBSERVATIONS:\n{tool_context}\n\nQUESTION:\n{query}"

    if sentiment_result["label"] == "frustrated" and sentiment_result["frustration_score"] >= 0.55:
        user_prompt += (
            "\n\nNOTE: The passenger appears frustrated. Prioritize empathy, acknowledge "
            "the inconvenience, and offer a clear escalation path to a human agent."
        )

    llm = get_primary_llm()
    answer = llm.generate(SYSTEM_PROMPT, user_prompt)

    if flight_number and tool_observations:
        answer = tool_observations[0] + "\n\n" + answer

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
            kes_amount, conversion_note = _to_kes(amount, currency)
            description = ("KQ-Propel ancillary fee settlement"
                           if conversion_note is None else
                           f"KQ-Propel ancillary fee settlement (USD {amount:,.0f} converted "
                           f"at {config.USD_TO_KES_RATE:,.2f} KES/USD)")
            database.set_pending_payment(session_id, kes_amount, description, "offered")
            if conversion_note:
                answer += "\n\n" + conversion_note
            answer += _offer_payment_message(kes_amount)
            trace.append({"step": "thought",
                           "content": (
                               f"Grounded payable fee of Ksh {kes_amount:,.0f} identified - "
                               f"offering to settle it by M-Pesa."
                               if conversion_note is None else
                               f"Grounded payable fee of USD {amount:,.0f} identified. Daraja "
                               f"settles in shillings, so it was converted at the declared "
                               f"prototype rate of {config.USD_TO_KES_RATE:,.2f} KES/USD to "
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

    trace.append({"step": "final_answer", "model": llm.name, "content": answer})

    metrics = evaluation.evaluate_response(message, answer, retrieved)
    sources = []
    for r in retrieved:
        source = (r.get("source") or "").replace(".txt", "").replace("_", " ").title()
        label = f"{source} - {r.get('section')}" if r.get("section") else source
        if label not in sources:
            sources.append(label)

    database.log_message(session_id, "user", message,
                          sentiment_label=sentiment_result["label"],
                          frustration_score=sentiment_result["frustration_score"])
    database.log_message(session_id, "assistant", answer, sources=", ".join(sources))
    database.log_rag_evaluation(query_id=str(uuid.uuid4()), query=message,
                                 context_relevance=metrics["context_relevance"],
                                 groundedness=metrics["groundedness"],
                                 answer_relevance=metrics["answer_relevance"], model=llm.name)

    return {
        "answer": answer,
        "model_used": llm.name,
        "sentiment": sentiment_result,
        "sources": sources,
        "trace": trace,
        "rag_metrics": metrics,
        "payment": payment_result,
    }
