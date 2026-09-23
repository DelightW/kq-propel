"""
RAG-Triad evaluation harness (Context Relevance, Groundedness, Answer
Relevance) as specified in the proposal's evaluation strategy.

When an OpenAI key is available these could be judged by an LLM-as-judge
(e.g. TruLens/RAGAS style). To keep the system fully self-contained and
reproducible offline, this module implements transparent, auditable proxies.

METRIC VALIDATION
-----------------
An earlier revision scored all three metrics by bag-of-words overlap.
Adversarial testing (tools/verify_findings.py) showed this was not measuring
what it claimed to measure:

  * Groundedness scored a *fabricated* figure ("Ksh 12,000" against a source
    stating 9,000) as 1.0, scored a direct contradiction ("there is no fee")
    as 1.0, and scored a *correct* paraphrase as 0.0. Because the offline
    generator is extractive it copies context verbatim, so the metric was
    pinned at 1.0 by construction rather than by merit.
  * Context Relevance averaged the blended BM25 + heading-boost ranking score,
    which is unbounded - observed above 1.4 - while being read as a proportion.
  * Answer Relevance scored a verbatim echo of the question 1.00 and the
    correct answer 0.50, because overlap was computed against the *question's*
    vocabulary. It rewarded restatement over resolution.

The implementations below address each failure directly. The adversarial cases
are retained as regression tests in tools/test_evaluation_metrics.py.
"""
import re
from typing import Dict, Iterable, List, Optional, Sequence

_WORD_RE = re.compile(r"[a-zA-Z0-9']{3,}")
_NUMBER_RE = re.compile(r"\d[\d,]*\.?\d*")

# Function words carry no grounding signal; counting them lets an answer look
# supported purely by sharing English with the context.
_STOPWORDS = frozenset("""
the and for are but not you your our their its with from this that these those
which who whom whose what when where how why all any both each few more most
other some such only own same than too very can will just should now have has
had was were been being does did doing would could may might must shall into
over under above below between during before after again further then once
here there about against through upon per via out off down
""".split())

# Negation cues for the polarity check in groundedness. A sentence that denies
# what the source asserts must not score as supported merely because it reuses
# the source's vocabulary.
_NEGATION_CUES = frozenset("""
no not never none cannot can't won't wont doesn't doesnt don't dont isn't isnt
aren't arent nothing neither nor without exempt waived unlimited
""".split())

_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90,
}
_MULTIPLIERS = {"hundred": 100, "thousand": 1000, "million": 1000000}


def _keywords(text: str) -> set:
    return set(w.lower() for w in _WORD_RE.findall(text))


def _content_words(text: str) -> set:
    return {w for w in _keywords(text) if w not in _STOPWORDS and not w.isdigit()}


def _numeric_atoms(text: str) -> set:
    """Every quantity asserted by `text`, normalised so '9,000', '9000' and
    'nine thousand' are recognised as the same claim.

    Quantities are the checkable core of a policy answer - a fee, a weight
    limit, a deadline - so they are what groundedness must verify."""
    atoms = set()

    for raw in _NUMBER_RE.findall(text):
        cleaned = raw.replace(",", "").rstrip(".")
        if not cleaned:
            continue
        try:
            atoms.add(float(cleaned))
        except ValueError:
            continue

    # Spelled-out quantities: "nine thousand shillings".
    current = running = 0
    seen_word = False
    for token in re.findall(r"[a-z]+", text.lower()):
        if token in _NUMBER_WORDS:
            current += _NUMBER_WORDS[token]
            seen_word = True
        elif token in _MULTIPLIERS:
            current = (current or 1) * _MULTIPLIERS[token]
            running += current
            current = 0
            seen_word = True
        else:
            if seen_word and (running + current):
                atoms.add(float(running + current))
            current = running = 0
            seen_word = False
    if seen_word and (running + current):
        atoms.add(float(running + current))

    return atoms


_NEGATION_TOKEN_RE = re.compile(r"[a-zA-Z']+")


def _has_negation(text: str) -> bool:
    # Deliberately not _keywords(): that requires 3+ characters and would miss
    # the single most common cue, "no".
    return bool({t.lower() for t in _NEGATION_TOKEN_RE.findall(text)} & _NEGATION_CUES)


def _best_aligned_sentence(sentence_content: set,
                           context_sentences: Sequence[str]) -> Optional[str]:
    """The context sentence sharing the most content vocabulary, used to decide
    whether a negation in the answer mirrors or contradicts the source."""
    best, best_overlap = None, 0
    for candidate in context_sentences:
        overlap = len(sentence_content & _content_words(candidate))
        if overlap > best_overlap:
            best, best_overlap = candidate, overlap
    return best


def context_relevance(retrieved_chunks: List[Dict]) -> float:
    """Mean query-chunk semantic similarity, bounded to [0, 1].

    Reads `dense_score` (the raw query-chunk cosine) rather than `score` (the
    blended BM25 + heading-boost value used for *ranking*). Ranking and
    measurement are deliberately kept separate: the blend is tuned to order
    results well and is unbounded, so averaging it produced values above 1.0
    that were then reported as a proportion.
    """
    if not retrieved_chunks:
        return 0.0
    scores = []
    for chunk in retrieved_chunks:
        value = chunk.get("dense_score")
        if value is None:
            # Rehydrated chunks without a separated cosine: fall back to the
            # blended score, clamped so the metric remains a proportion.
            value = chunk.get("score", 0.0)
        scores.append(min(max(float(value), 0.0), 1.0))
    return round(sum(scores) / len(scores), 3)


def groundedness(answer: str, context: str) -> float:
    """Proportion of answer sentences whose claims are supported by the context.

    A sentence is supported only if it passes all three tests:

      1. Quantity check - every quantity it asserts appears in the context.
         This rejects a fabricated fee.
      2. Polarity check - it does not negate something the aligned context
         asserts. This rejects a contradiction that reuses the source's words.
      3. Topicality check - it shares meaningful content vocabulary with the
         context, ignoring function words.

    A correct paraphrase passes via (1) even when its wording diverges, which
    is why numeric agreement is treated as decisive evidence rather than as
    one more overlapping token.

    Sentences that assert nothing checkable about the retrieved subject matter
    - the agent's empathy preamble, "I'll help you sort this out right away" -
    are not grounding claims and are excluded from the denominator. Counting
    them would report conversational courtesy as hallucination.
    """
    if not answer.strip():
        return 0.0
    context = context or ""
    context_content = _content_words(context)
    context_numbers = _numeric_atoms(context)
    if not context_content and not context_numbers:
        return 0.0

    context_sentences = [s for s in re.split(r"(?<=[.!?])\s+", context) if s.strip()]

    grounded = 0
    considered = 0
    for sentence in re.split(r"(?<=[.!?])\s+", answer):
        if not _keywords(sentence):
            continue

        sentence_numbers = _numeric_atoms(sentence)
        sentence_content = _content_words(sentence)

        # Claim-bearing test: asserts a quantity, or says something about the
        # subject matter the context covers.
        if not sentence_numbers and not (sentence_content & context_content):
            continue

        considered += 1

        if sentence_numbers - context_numbers:
            continue  # asserts a quantity the source does not contain

        overlap = (len(sentence_content & context_content) / len(sentence_content)
                   if sentence_content else 0.0)

        supported_numbers = sentence_numbers & context_numbers
        if overlap < 0.35 and not supported_numbers:
            continue

        if _has_negation(sentence):
            aligned = _best_aligned_sentence(sentence_content, context_sentences)
            if aligned is not None and not _has_negation(aligned):
                continue  # denies what the source asserts

        grounded += 1

    if considered == 0:
        return 0.0
    return round(grounded / considered, 3)


def answer_relevance(answer: str, question: str,
                     expected_keywords: Optional[Iterable[str]] = None) -> float:
    """How well the answer actually resolves the question.

    When the evaluation dataset supplies `expected_keywords` - the gold answer
    keys, each verified answerable by docgen/generate_datasets.py - relevance
    is their coverage in the answer. This is the preferred path: it measures
    resolution directly rather than by proxy.

    Without gold keys (the live chat path has no ground truth) it falls back to
    question coverage *discounted by novelty*, so an answer that merely
    restates the question cannot score well.
    """
    if not answer.strip():
        return 0.0

    if expected_keywords:
        expected = [str(k).strip() for k in expected_keywords if str(k).strip()]
        if expected:
            answer_numbers = _numeric_atoms(answer)
            answer_lower = answer.lower()
            hits = 0
            for key in expected:
                if key.lower() in answer_lower:
                    hits += 1
                    continue
                key_numbers = _numeric_atoms(key)
                if key_numbers and key_numbers <= answer_numbers:
                    hits += 1
            return round(hits / len(expected), 3)

    q_kw = _content_words(question)
    a_kw = _content_words(answer)
    if not q_kw or not a_kw:
        return 0.0

    coverage = min(len(a_kw & q_kw) / len(q_kw), 1.0)
    # Proportion of the answer that is not the question repeated back.
    novelty = len(a_kw - q_kw) / len(a_kw)
    return round(coverage * novelty, 3)


def evaluate_response(question: str, answer: str, retrieved_chunks: List[Dict],
                      expected_keywords: Optional[Iterable[str]] = None) -> Dict:
    # Section headings and source names are part of the grounding evidence and
    # appear in the composed citation, so they count as context.
    context = " ".join(
        " ".join(str(c.get(field, "")) for field in ("source", "section", "text"))
        for c in retrieved_chunks
    )
    return {
        "context_relevance": context_relevance(retrieved_chunks),
        "groundedness": groundedness(answer, context),
        "answer_relevance": answer_relevance(answer, question, expected_keywords),
    }
