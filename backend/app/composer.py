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

# Appended whenever an answer rests on a prototype-authored document, so the
# system discloses the status of its own evidence rather than presenting
# synthetic scaffolding as airline policy.
SYNTHETIC_SOURCE_NOTE = (
    "Note: this answer draws on a document written for this research prototype "
    "rather than on published Kenya Airways policy."
)
_RANGE_RE = re.compile(
    r"between\s+(\d+)\s*(kg|hours?|hrs?|days?|characters?)?\s+and\s+(\d+)\s*(kg|hours?|hrs?|days?|characters?)?",
    re.IGNORECASE,
)
_OVER_RE = re.compile(r"(?:more than|over|exceeding|greater than|beyond)\s+(\d+)\s*(kg|hours?|hrs?|days?)", re.IGNORECASE)
# "a delay of four to eight hours": a band written as "X to Y", the unit after Y.
_TO_RANGE_RE = re.compile(r"\b(\d+)\s+to\s+(\d+)\s*(kg|hours?|hrs?|days?)\b", re.IGNORECASE)
# Policy text writes small numbers in words; the bands compare digits.
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                 "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "twenty-four": 24,
                 "twenty four": 24, "forty-eight": 48, "seventy-two": 72}
_NUMBER_WORD_RE = re.compile(r"\b(" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")\b",
                             re.IGNORECASE)
# "Section 4: Size and Weight Conditions" - the number orders the sections; it
# says nothing about the subject, and matched "delayed 4 hours".
_SECTION_NUMBER_RE = re.compile(r"^\s*section\s+\d+\s*:\s*", re.IGNORECASE)


def _digits(text: str) -> str:
    return _NUMBER_WORD_RE.sub(lambda m: str(_NUMBER_WORDS[m.group(1).lower()]), text)
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
    sentence = _digits(sentence)
    bonus = 0.0
    for r_match in _TO_RANGE_RE.finditer(sentence):
        low, high = float(r_match.group(1)), float(r_match.group(2))
        unit = _canon_unit(r_match.group(3))
        for q in quantities:
            if unit and q["unit"] and unit != q["unit"]:
                continue
            bonus += 6.0 if low <= q["value"] <= high else -2.0
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
    """'Refund Policy - Section 5: Name Corrections (version 1.0, as published
    23 Sep 2026)'. The edition says which version of the policy was relied on."""
    from app import provenance
    section = (chunk.get("section") or "").strip()
    raw_source = chunk.get("source") or ""
    source = raw_source.replace(".txt", "").replace("_", " ").title()
    if section and not section.lower().startswith(source.lower()):
        label = f"{source} - {section}"
    else:
        label = section or source
    edition = provenance.edition_label(raw_source)
    return f"{label} ({edition})" if edition else label


def _select(question: str, chunks: List[Dict], max_sentences: int) -> List[Dict]:
    """The policy sentences that best answer one question, best first."""
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
        section_tokens = set(retrieval.tokenize(_SECTION_NUMBER_RE.sub("", chunk.get("section") or "")))
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
        return []

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
    return selected


# A second question joined to the first: "...and what will the board cost?"
_PART_JOIN_RE = re.compile(
    r"\s*,?\s+(?:and|also|plus)\s+(?=(?:what|when|how|is|are|can|could|do|does|will|would|"
    r"where|which|who|why|should|must)\b)", re.IGNORECASE)
_QUESTION_START_RE = re.compile(
    r"^(?:what|when|how|is|are|can|could|do|does|will|would|where|which|who|why|should|must)\b",
    re.IGNORECASE)
# Most sentences a multi-part answer may use, across all its parts.
_MULTI_PART_MAX_SENTENCES = 5
# Subject words a part needs before it is searched on its own.
_SELF_CONTAINED_PART_TOKENS = 2


def split_question(question: str) -> List[str]:
    """The separate questions inside a message, each carrying the situation
    the passenger described.

    "I am checking in for an international flight with a surfboard. When must
    I be at the airport and what will the board cost?" becomes two questions,
    each prefixed with the first sentence. A message with one question is
    returned unchanged.
    """
    sentences = [x.strip() for x in _SENTENCE_SPLIT_RE.split(question.strip()) if x.strip()]
    situation, parts = [], []
    for sentence in sentences:
        pieces = [x.strip(" ,") for x in _PART_JOIN_RE.split(sentence) if x.strip(" ,")]
        if sentence.endswith("?") or len(pieces) > 1 or _QUESTION_START_RE.match(sentence):
            parts.extend(pieces)
        else:
            situation.append(sentence)
    if len(parts) < 2:
        return [question]
    # The described situation is attached only to a part too short to name
    # its own subject ("What am I owed?"). Attached to every part, it pulled
    # "what will the board cost?" back toward the check-in sentence the
    # situation was about.
    context = " ".join(situation)
    return [part if len(retrieval.topical_tokens(part)) >= _SELF_CONTAINED_PART_TOKENS
            else f"{context} {part}".strip()
            for part in parts]


def compose_answer(question: str, chunks: List[Dict], max_sentences: int = 3,
                    verbose: bool = True) -> str:
    """Builds a concise, fully grounded answer from retrieved chunks.

    A message that asks two things is answered part by part, so the second
    question is not crowded out by the section that best answers the first.
    """
    if not chunks:
        return ("I could not find that in the official policy documents I have access to. "
                "I can help with baggage allowances and fees, flight delays and compensation, "
                "refunds and ticket changes, and check-in or boarding rules.")

    parts = split_question(question) if verbose else [question]
    if len(parts) > 1:
        selected, seen = [], set()
        for part in parts:
            for cand in _select(part, chunks, max_sentences=2):
                key = cand["sentence"].lower()
                if key not in seen:
                    seen.add(key)
                    selected.append(cand)
        selected = selected[:_MULTI_PART_MAX_SENTENCES]
    else:
        selected = _select(question, chunks, max_sentences)

    if not selected:
        top = chunks[0]
        first = next((_clean_sentence(s) for s in _SENTENCE_SPLIT_RE.split(top["text"])
                       if _clean_sentence(s) and not _is_heading(_clean_sentence(s))), "")
        label = _section_label(top)
        return f"{first}\n\nSource: {label}" if first else (
            "I could not find a specific answer to that in the policy documents.")


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
    if any(c["chunk"].get("provenance") == "synthetic" for c in selected):
        answer += "\n\n" + SYNTHETIC_SOURCE_NOTE
    return answer
