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
from typing import Dict, List, Optional

from app import aviationstack, composer, daraja, database, evaluation, sentiment
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
_HELP_RE = re.compile(r"\b(what can you do|who are you|what are you|help me|how do you work|"
                       r"what do you do|capabilities)\b", re.IGNORECASE)

# Transactional phrasing actually initiates an STK push; informational
# phrasing ("how do I pay?") is answered from policy instead, so the agent
# never charges a passenger who only asked a question.
_TRANSACTIONAL_RE = re.compile(
    r"\b(pay\s+now|pay\s+it|pay\s+the\s+fee|pay\s+for\s+it|make\s+the\s+payment|"
    r"send\s+(?:me\s+)?(?:the\s+)?(?:stk|prompt|push|request)|initiate\s+(?:the\s+)?payment|"
    r"charge\s+me|i\s+(?:want|would\s+like|wish)\s+to\s+pay|let'?s\s+pay|go\s+ahead\s+and\s+pay|"
    r"process\s+(?:the\s+)?payment)\b", re.IGNORECASE)
_PAYMENT_TOPIC_RE = re.compile(r"\b(pay|payment|m-?pesa|mpesa|stk|till|settle)\b", re.IGNORECASE)

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

    # --- Thought 2: conversational intent shortcut ---
    small_talk = _small_talk_reply(message)
    if small_talk:
        trace.append({"step": "thought",
                       "content": "Conversational intent detected - no document retrieval needed."})
        trace.append({"step": "final_answer", "model": "rule-based", "content": small_talk})
        database.log_message(session_id, "user", message,
                              sentiment_label=sentiment_result["label"],
                              frustration_score=sentiment_result["frustration_score"])
        database.log_message(session_id, "assistant", small_talk)
        return {
            "answer": small_talk, "model_used": "rule-based",
            "sentiment": sentiment_result, "sources": [], "trace": trace,
            "rag_metrics": {}, "payment": None,
        }

    # --- Action: hybrid semantic + lexical retrieval over policy documents ---
    store = get_vector_store()
    retrieved = store.similarity_search(message, k=4)
    trace.append({
        "step": "action",
        "tool": "retrieve_policy_documents",
        "input": message,
        "observation": [
            {"source": r.get("source"), "section": r.get("section"),
             "score": r.get("score"), "excerpt": r["text"][:160]}
            for r in retrieved
        ],
    })

    tool_observations = []

    # --- Action: live flight telemetry tool (AviationStack) ---
    flight_number = _detect_flight_number(message)
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
    if _TRANSACTIONAL_RE.search(message):
        phone_match = _PHONE_RE.search(message)
        amount_match = _AMOUNT_RE.search(message)
        if amount_match:
            amount = float(amount_match.group(1).replace(",", ""))
        else:
            amount = _extract_fee_from_context(retrieved, message)

        if amount is None:
            tool_observations.append(
                "Payment requested but the applicable fee could not be determined from "
                "policy. Ask the passenger to confirm the amount."
            )
            trace.append({"step": "thought",
                           "content": "Payment intent detected but no grounded amount available."})
        elif not phone_match:
            tool_observations.append(
                f"Payment of Ksh {amount:,.0f} is ready to process. Ask the passenger for the "
                f"M-Pesa phone number to send the STK push to."
            )
            trace.append({"step": "thought",
                           "content": f"Payment intent detected for Ksh {amount:,.0f}; awaiting phone number."})
        else:
            phone = "254" + phone_match.group(1)
            reference = f"KQPROPEL-{uuid.uuid4().hex[:8].upper()}"
            payment_result = daraja.initiate_stk_push(
                phone_number=phone, amount=amount, reference=reference,
                description="KQ-Propel ancillary fee settlement",
            )
            trace.append({"step": "action", "tool": "initiate_mpesa_stk_push",
                           "input": {"phone": phone, "amount": amount},
                           "observation": payment_result})
            tool_observations.append(f"Payment tool result: {payment_result.get('message')}")
            database.log_transaction(
                session_id=session_id,
                checkout_request_id=payment_result.get("checkout_request_id", ""),
                phone_number=phone, amount=amount, reference=reference,
                description="Ancillary fee settlement",
                status="initiated" if payment_result.get("success") else "failed",
            )
    elif _PAYMENT_TOPIC_RE.search(message):
        tool_observations.append(
            "The passenger is asking about payment options. Explain them, and mention that "
            "I can send an M-Pesa STK push directly in this chat if they say they want to pay."
        )

    # --- Compose the final grounded answer ---
    context_text = _serialize_context(retrieved)
    tool_context = "\n".join(tool_observations)
    user_prompt = f"CONTEXT:\n{context_text}\n\nTOOL OBSERVATIONS:\n{tool_context}\n\nQUESTION:\n{message}"

    if sentiment_result["label"] == "frustrated" and sentiment_result["frustration_score"] >= 0.55:
        user_prompt += (
            "\n\nNOTE: The passenger appears frustrated. Prioritize empathy, acknowledge "
            "the inconvenience, and offer a clear escalation path to a human agent."
        )

    llm = get_primary_llm()
    answer = llm.generate(SYSTEM_PROMPT, user_prompt)

    # Sentiment-aware framing (corrections requirement): a frustrated
    # passenger gets acknowledgement and an explicit escalation path.
    if sentiment_result["label"] == "frustrated" and sentiment_result["frustration_score"] >= 0.55:
        answer = (
            "I'm really sorry for the trouble - I understand how frustrating this is, "
            "and I'll help you sort it out right away.\n\n" + answer +
            "\n\nIf this doesn't fully resolve things, I can escalate you to a human "
            "support agent straight away - just say \"escalate\"."
        )

    if flight_number and tool_observations:
        answer = tool_observations[0] + "\n\n" + answer

    if payment_result and payment_result.get("message"):
        answer += "\n\n" + payment_result["message"]

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
