"""
ReAct (Reasoning -> Action -> Observation) agent orchestrator.

Implements the Thought -> Action -> Observation loop described in the
proposal: the agent inspects the passenger's message, decides whether it
needs to call a tool (policy retrieval, live flight telemetry, or an M-Pesa
STK push payment), executes it, observes the result, and only then composes
a final grounded answer with the primary LLM. Every step is captured in a
transparent trace so the admin dashboard / chat UI can display the agent's
reasoning (supporting the "zero hallucination" / groundedness goals).
"""
import re
import uuid
from typing import Dict, List

from app import aviationstack, daraja, evaluation, sentiment
from app.llm import get_primary_llm
from app.vectorstore import get_vector_store

_FLIGHT_RE = re.compile(r"\bKQ\s?-?\s?(\d{2,4})\b", re.IGNORECASE)
_PAYMENT_KEYWORDS = ["pay", "m-pesa", "mpesa", "stk", "payment", "settle the fee", "pay the fee"]
_PHONE_RE = re.compile(r"(2547\d{8}|07\d{8}|01\d{8})")
_AMOUNT_RE = re.compile(r"(?:ksh|kes)\s?([\d,]+)", re.IGNORECASE)

SYSTEM_PROMPT = (
    "You are KQ-Propel, an aviation passenger-support assistant. Answer strictly "
    "and only using the CONTEXT provided (retrieved from official policy "
    "documents) and any TOOL OBSERVATIONS supplied. Never invent fees, dates or "
    "policy details that are not present in the context. If the context does not "
    "contain the answer, say so plainly. Be concise, empathetic, and cite the "
    "relevant policy section when possible."
)


def _detect_flight_number(message: str):
    match = _FLIGHT_RE.search(message)
    if match:
        return f"KQ{match.group(1)}"
    return None


def _wants_payment(message: str) -> bool:
    lower = message.lower()
    return any(kw in lower for kw in _PAYMENT_KEYWORDS)


def run_agent_turn(session_id: str, message: str) -> Dict:
    trace: List[Dict] = []

    # --- Thought 1: does this require sentiment/frustration awareness? ---
    sentiment_result = sentiment.classify_frustration(message)
    trace.append({
        "step": "thought",
        "content": f"Classify passenger tone before responding. Detected: "
                   f"{sentiment_result['label']} (score={sentiment_result['frustration_score']})",
    })

    # --- Action: semantic retrieval over policy documents (RAG) ---
    store = get_vector_store()
    retrieved = store.similarity_search(message, k=3)
    trace.append({
        "step": "action",
        "tool": "retrieve_policy_documents",
        "input": message,
        "observation": [
            {"source": r["source"], "score": round(r["score"], 3), "excerpt": r["text"][:160]}
            for r in retrieved
        ],
    })

    tool_observations = []

    # --- Action: live flight telemetry tool (AviationStack) ---
    flight_number = _detect_flight_number(message)
    if flight_number:
        flight_status = aviationstack.get_flight_status(flight_number)
        trace.append({"step": "action", "tool": "get_flight_status", "input": flight_number,
                       "observation": flight_status})
        tool_observations.append(f"Flight telemetry for {flight_number}: {flight_status}")

    # --- Action: payment tool (Safaricom Daraja STK push) ---
    payment_result = None
    if _wants_payment(message):
        phone_match = _PHONE_RE.search(message)
        amount_match = _AMOUNT_RE.search(message)
        phone = phone_match.group(1) if phone_match else "254700000000"
        amount = float(amount_match.group(1).replace(",", "")) if amount_match else 5000.0
        payment_result = daraja.initiate_stk_push(
            phone_number=phone, amount=amount, reference=f"KQPROPEL-{uuid.uuid4().hex[:8]}",
            description="KQ-Propel ancillary fee settlement",
        )
        trace.append({"step": "action", "tool": "initiate_mpesa_stk_push",
                       "input": {"phone": phone, "amount": amount}, "observation": payment_result})
        tool_observations.append(f"Payment tool result: {payment_result}")
        from app import database
        database.log_transaction(
            session_id=session_id,
            checkout_request_id=payment_result.get("checkout_request_id", ""),
            phone_number=phone, amount=amount,
            reference=f"KQPROPEL-{uuid.uuid4().hex[:6]}",
            description="Ancillary fee settlement", status="initiated" if payment_result.get("success") else "failed",
        )

    # --- Compose final grounded answer ---
    context_text = "\n---\n".join(r["text"] for r in retrieved)
    tool_context = "\n".join(tool_observations)
    user_prompt = (
        f"CONTEXT:\n{context_text}\n\nTOOL OBSERVATIONS:\n{tool_context}\n\nQUESTION:\n{message}"
    )
    # Escalation guidance based on frustration level, per the sentiment-aware
    # response requirement in the corrections document.
    if sentiment_result["label"] == "frustrated" and sentiment_result["frustration_score"] >= 0.6:
        user_prompt += (
            "\n\nNOTE: The passenger appears frustrated. Prioritize empathy, "
            "acknowledge the inconvenience, and offer a clear escalation path "
            "to a human agent if the issue cannot be fully resolved."
        )

    llm = get_primary_llm()
    answer = llm.generate(SYSTEM_PROMPT, user_prompt)
    trace.append({"step": "final_answer", "model": llm.name, "content": answer})

    metrics = evaluation.evaluate_response(message, answer, retrieved)

    from app import database
    database.log_message(session_id, "user", message,
                          sentiment_label=sentiment_result["label"],
                          frustration_score=sentiment_result["frustration_score"])
    database.log_message(session_id, "assistant", answer,
                          sources=", ".join(sorted({r["source"] for r in retrieved})))
    database.log_rag_evaluation(query_id=str(uuid.uuid4()), query=message,
                                 context_relevance=metrics["context_relevance"],
                                 groundedness=metrics["groundedness"],
                                 answer_relevance=metrics["answer_relevance"], model=llm.name)

    return {
        "answer": answer,
        "model_used": llm.name,
        "sentiment": sentiment_result,
        "sources": sorted({r["source"] for r in retrieved}),
        "trace": trace,
        "rag_metrics": metrics,
        "payment": payment_result,
    }
