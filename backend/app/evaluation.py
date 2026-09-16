"""
RAG-Triad evaluation harness (Context Relevance, Groundedness, Answer
Relevance) as specified in the proposal's evaluation strategy.

When an OpenAI key is available these could be judged by an LLM-as-judge
(e.g. TruLens/RAGAS style). To keep the system fully self-contained and
reproducible offline, this module implements transparent heuristic proxies:

- Context Relevance: mean cosine similarity between the query and the
  retrieved chunks actually used to ground the answer.
- Groundedness: proportion of answer sentences that share significant
  vocabulary overlap with the retrieved context (a proxy for "every claim is
  traceable to a retrieved document chunk").
- Answer Relevance: vocabulary overlap between the answer and the original
  question, rewarding direct resolution over deflection.
"""
import re
from typing import Dict, List

_WORD_RE = re.compile(r"[a-zA-Z0-9']{3,}")


def _keywords(text: str) -> set:
    return set(w.lower() for w in _WORD_RE.findall(text))


def context_relevance(retrieved_chunks: List[Dict]) -> float:
    if not retrieved_chunks:
        return 0.0
    scores = [c.get("score", 0.0) for c in retrieved_chunks]
    return round(sum(scores) / len(scores), 3)


def groundedness(answer: str, context: str) -> float:
    if not answer.strip():
        return 0.0
    sentences = re.split(r"(?<=[.!?])\s+", answer)
    context_kw = _keywords(context)
    if not context_kw:
        return 0.0
    grounded = 0
    considered = 0
    for sentence in sentences:
        s_kw = _keywords(sentence)
        if not s_kw:
            continue
        considered += 1
        overlap = len(s_kw & context_kw) / max(len(s_kw), 1)
        if overlap >= 0.25:
            grounded += 1
    if considered == 0:
        return 0.0
    return round(grounded / considered, 3)


def answer_relevance(answer: str, question: str) -> float:
    a_kw, q_kw = _keywords(answer), _keywords(question)
    if not q_kw:
        return 0.0
    overlap = len(a_kw & q_kw) / len(q_kw)
    return round(min(overlap * 1.5, 1.0), 3)


def evaluate_response(question: str, answer: str, retrieved_chunks: List[Dict]) -> Dict:
    context = " ".join(c["text"] for c in retrieved_chunks)
    return {
        "context_relevance": context_relevance(retrieved_chunks),
        "groundedness": groundedness(answer, context),
        "answer_relevance": answer_relevance(answer, question),
    }
