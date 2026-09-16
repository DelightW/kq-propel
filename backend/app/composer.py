"""
Grounded answer composition.

Used by the offline/deterministic model path (and as a safety net generally).
Rather than generating free-form prose that could hallucinate fees or dates,
this module selects the specific policy sentences that answer the passenger's
question and formats them into a short, direct reply with a section citation.

Two behaviours matter most for answer quality:

* Numeric-band matching - a passenger saying "my bag is 5kg over" must be
  matched to the "between 1kg and 10kg" fee band rather than any other band.
* Sentence-level selection - only the sentences that actually answer the
  question are returned, instead of the whole retrieved section.
"""
import re
from typing import Dict, List, Optional

from app import retrieval

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_RANGE_RE = re.compile(
    r"between\s+(\d+)\s*(kg|hours?|hrs?|days?|characters?)?\s+and\s+(\d+)\s*(kg|hours?|hrs?|days?|characters?)?",
    re.IGNORECASE,
)
_OVER_RE = re.compile(r"(?:more than|over|exceeding|greater than)\s+(\d+)\s*(kg|hours?|hrs?|days?)", re.IGNORECASE)
_NEGATED_OVER_RE = re.compile(r"\b(?:not|never|without)\s+(?:\w+\s+){0,2}?(?:more than|over|exceeding|greater than)\s+(\d+)",
                                re.IGNORECASE)
_QUERY_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(kg|kilos?|kilograms?|hours?|hrs?|days?)", re.IGNORECASE)

_UNIT_CANON = {
    "kg": "kg", "kilo": "kg", "kilos": "kg", "kilogram": "kg", "kilograms": "kg",
    "hour": "hour", "hours": "hour", "hr": "hour", "hrs": "hour",
    "day": "day", "days": "day",
    "character": "character", "characters": "character",
}


def _canon_unit(unit: Optional[str]) -> Optional[str]:
    if not unit:
        return None
    return _UNIT_CANON.get(unit.lower().rstrip("."), unit.lower())


def _query_quantities(question: str) -> List[Dict]:
    out = []
    for match in _QUERY_NUM_RE.finditer(question):
        out.append({"value": float(match.group(1)), "unit": _canon_unit(match.group(2))})
    return out


def _numeric_band_bonus(sentence: str, quantities: List[Dict]) -> float:
    """Rewards sentences whose stated numeric band contains a quantity the
    passenger mentioned, and penalises bands that explicitly exclude it."""
    if not quantities:
        return 0.0
    bonus = 0.0
    for r_match in _RANGE_RE.finditer(sentence):
        low = float(r_match.group(1))
        high = float(r_match.group(3))
        unit = _canon_unit(r_match.group(2) or r_match.group(4))
        for q in quantities:
            if unit and q["unit"] and unit != q["unit"]:
                continue
            if low <= q["value"] <= high:
                bonus += 6.0
            else:
                bonus -= 2.0
    for o_match in _OVER_RE.finditer(sentence):
        threshold = float(o_match.group(1))
        unit = _canon_unit(o_match.group(2))
        # "not exceeding 10kg" states an allowance ceiling, not a fee band -
        # treating it as a match would surface the wrong policy rule.
        if any(float(n) == threshold for n in _NEGATED_OVER_RE.findall(sentence)):
            continue
        for q in quantities:
            if unit and q["unit"] and unit != q["unit"]:
                continue
            if q["value"] > threshold:
                bonus += 3.0
            else:
                bonus -= 1.0
    return bonus


def _clean_sentence(sentence: str) -> str:
    return " ".join(sentence.split()).strip()


def _is_heading(sentence: str) -> bool:
    stripped = sentence.strip()
    return bool(re.match(r"^Section\s+\d+\s*:\s*[^.]*$", stripped, re.IGNORECASE)) or \
        (len(stripped.split()) <= 8 and not stripped.endswith("."))


def _section_label(chunk: Dict) -> str:
    section = (chunk.get("section") or "").strip()
    source = (chunk.get("source") or "").replace(".txt", "").replace("_", " ").title()
    if section and not section.lower().startswith(source.lower()):
        return f"{source} - {section}"
    return section or source


def compose_answer(question: str, chunks: List[Dict], max_sentences: int = 3,
                    verbose: bool = True) -> str:
    """Builds a concise, fully grounded answer from retrieved chunks."""
    if not chunks:
        return ("I could not find that in the official policy documents I have access to. "
                "I can help with baggage allowances and fees, flight delays and compensation, "
                "refunds and ticket changes, and check-in or boarding rules.")

    quantities = _query_quantities(question)
    base_q_tokens = set(retrieval.tokenize(question))
    q_tokens = set(retrieval.expand_query(retrieval.tokenize(question)))

    candidates = []
    for rank, chunk in enumerate(chunks):
        # A matching section heading ("Refund Processing Timelines") is strong
        # evidence that the whole section answers the question, independent of
        # how many query words happen to appear in any single sentence. The
        # sentence that actually answers "what is the baggage allowance?" is
        # "...one checked bag not exceeding 23kg", which shares no words with
        # the question at all - only its heading does.
        section_tokens = set(retrieval.tokenize(chunk.get("section") or ""))
        section_expanded = set(retrieval.expand_query(section_tokens))
        heading_hits = len(q_tokens & section_tokens)
        heading_coverage = len(base_q_tokens & section_expanded) / max(len(base_q_tokens), 1)
        heading_bonus = heading_hits * 1.5 + heading_coverage * 3.0
        body = chunk["text"]
        for sentence in _SENTENCE_SPLIT_RE.split(body):
            sentence = _clean_sentence(sentence)
            if not sentence or _is_heading(sentence):
                continue
            s_tokens = set(retrieval.tokenize(sentence))
            if not s_tokens:
                continue
            overlap = len(q_tokens & s_tokens)
            coverage = overlap / max(len(q_tokens), 1)
            score = overlap + coverage * 2.0
            score += heading_bonus
            score += _numeric_band_bonus(sentence, quantities)
            # Weight by how strongly the parent chunk matched overall, so a
            # sentence from a weaker section cannot outrank the section that
            # retrieval identified as the best answer.
            score += float(chunk.get("score") or 0.0) * 4.0
            score += max(0.0, 1.0 - rank * 0.25)
            if overlap == 0:
                continue
            candidates.append({"sentence": sentence, "score": score, "chunk": chunk})

    if not candidates:
        top = chunks[0]
        first = next((_clean_sentence(s) for s in _SENTENCE_SPLIT_RE.split(top["text"])
                       if _clean_sentence(s) and not _is_heading(_clean_sentence(s))), "")
        label = _section_label(top)
        return f"{first}\n\nSource: {label}" if first else (
            "I could not find a specific answer to that in the policy documents.")

    candidates.sort(key=lambda c: c["score"], reverse=True)

    best_cand = candidates[0]
    best = best_cand["score"]
    best_chunk_id = id(best_cand["chunk"])

    selected: List[Dict] = []
    seen = set()
    for cand in candidates:
        key = cand["sentence"].lower()
        if key in seen:
            continue
        if selected:
            # Supporting sentences must stay close to the leading answer.
            # A sentence pulled from a *different* policy section has to be
            # near-equally relevant, otherwise an unrelated section gets
            # appended as filler.
            floor = 0.7 if id(cand["chunk"]) == best_chunk_id else 0.92
            if cand["score"] < best * floor:
                continue
        seen.add(key)
        selected.append(cand)
        if len(selected) >= max_sentences:
            break

    if not verbose:
        selected = selected[:1]

    answer = " ".join(c["sentence"] for c in selected)

    labels = []
    for c in selected:
        label = _section_label(c["chunk"])
        if label and label not in labels:
            labels.append(label)
    if labels:
        answer += "\n\nSource: " + "; ".join(labels)
    return answer
