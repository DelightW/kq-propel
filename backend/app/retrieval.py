"""
Hybrid retrieval scoring.

Dense vector similarity alone performs poorly on short, fact-dense policy
text (e.g. distinguishing "1kg to 10kg over" from "11kg to 20kg over"), so
retrieval combines:

  * BM25 lexical scoring - strong on exact policy terminology and numbers.
  * Dense cosine similarity - captures paraphrasing and semantic variance.

The two normalised scores are blended, which materially improves Context
Relevance in the RAG-Triad evaluation compared to either signal alone.
"""
import math
import re
from collections import Counter
from typing import Dict, Iterable, List

_WORD_RE = re.compile(r"[a-z0-9]+")

_STOPWORDS = {
    "the", "and", "for", "are", "was", "with", "that", "this", "from", "have",
    "has", "not", "but", "you", "your", "our", "can", "will", "how", "what",
    "when", "who", "why", "does", "did", "a", "an", "of", "to", "in", "on",
    "is", "it", "i", "my", "me", "be", "at", "as", "or", "if", "do", "am",
}

# Domain synonyms let a passenger's everyday phrasing match formal policy
# wording (e.g. "suitcase" -> "baggage", "cash back" -> "refund").
_SYNONYMS = {
    "bag": ["baggage", "luggage"],
    "bags": ["baggage", "luggage"],
    "baggage": ["bag", "bags", "luggage"],
    "suitcase": ["baggage", "luggage"],
    "luggage": ["baggage"],
    "allowance": ["allowed", "permitted", "limit"],
    "allowed": ["allowance", "permitted", "limit"],
    "permitted": ["allowance", "allowed", "limit"],
    "overweight": ["overweight", "exceeding", "over"],
    "heavy": ["overweight", "exceeding"],
    "fee": ["fee", "charge", "cost"],
    "cost": ["fee", "charge"],
    "charge": ["fee"],
    "price": ["fee", "charge"],
    "money": ["refund", "fee", "payment"],
    "cancel": ["cancellation", "cancelled"],
    "cancelled": ["cancellation"],
    "delay": ["delayed", "delay"],
    "delayed": ["delay"],
    "late": ["delay", "delayed"],
    "compensation": ["compensation", "entitled", "voucher"],
    "pay": ["payment", "pay", "mpesa"],
    "mpesa": ["m-pesa", "mpesa", "payment", "stk"],
    "refund": ["refund", "refundable"],
    "checkin": ["check-in", "checkin"],
    "check": ["check-in", "checkin"],
    "lost": ["lost", "delayed", "missing", "traced"],
    "missing": ["lost", "traced", "irregularity"],
    "document": ["passport", "visa", "documentation"],
    "documents": ["passport", "visa", "documentation"],
    "passport": ["passport", "documentation"],
    "golf": ["sporting", "golf"],
    "sport": ["sporting"],
    "sports": ["sporting"],
    "wheelchair": ["assistance", "wheelchair"],
    "child": ["minor", "infant"],
    "baby": ["infant"],
    "pet": ["special"],
    "change": ["change", "rebooking", "changes"],
    "reschedule": ["rebooking", "change"],
    # Duration phrasing ("how long does X take?") must reach the policy
    # sections that state processing timelines.
    "long": ["processing", "processed", "timelines", "within", "business", "days"],
    "take": ["processed", "processing", "within", "timelines"],
    "takes": ["processed", "processing", "within", "timelines"],
    "duration": ["processing", "timelines", "within"],
    "timeline": ["timelines", "processing", "within"],
    "timelines": ["processing", "within"],
    "soon": ["processing", "within", "days"],
}

# Multi-word phrases are collapsed *before* tokenisation, so a phrasal verb is
# never mistaken for its component words. Critically, "take off" means depart -
# it must not be expanded through the "take" -> processing-time synonyms used
# for questions like "how long does a refund take?".
_PHRASE_REWRITES = [
    (re.compile(r"\btak(?:e|es|ing)[\s-]?off\b", re.IGNORECASE), "departure"),
    (re.compile(r"\btake[\s-]?off\b", re.IGNORECASE), "departure"),
    (re.compile(r"\bcheck[\s-]?in\b", re.IGNORECASE), "checkin"),
    (re.compile(r"\bm[\s-]?pesa\b", re.IGNORECASE), "mpesa"),
    (re.compile(r"\bcash\s+back\b", re.IGNORECASE), "refund"),
    (re.compile(r"\bcarry[\s-]?on\b", re.IGNORECASE), "cabin"),
    (re.compile(r"\bno[\s-]?show\b", re.IGNORECASE), "noshow"),
]


def normalize_phrases(text: str) -> str:
    for pattern, replacement in _PHRASE_REWRITES:
        text = pattern.sub(replacement, text)
    return text


_INTERROGATIVE_RE = re.compile(
    r"\b(what|where|when|why|which|who|whose|how|can|could|may|might|will|would|"
    r"shall|should|do|does|did|is|are|was|were|am|have|has|had|please|tell\s+me|"
    r"i\s+need|i\s+want|help\s+me)\b", re.IGNORECASE)


def focus_query(text: str) -> str:
    """Narrows a message to the clause that actually carries the request.

    Passengers frequently prefix a question with an emotional statement - "I
    HAVE WAITED FOR 2 HOURS?! What is the baggage allowance?" - and those
    words are real tokens that pull retrieval toward delay and compensation
    sections. The emotional prefix is what the frustration classifier is for;
    retrieval should see only the request itself.
    """
    text = (text or "").strip()
    clauses = [c.strip() for c in re.split(r"(?<=[.!?])\s+|\n+", text) if c.strip()]
    if len(clauses) < 2:
        return text

    questions = [c for c in clauses if _INTERROGATIVE_RE.search(c)]
    if not questions:
        return text
    # The request is nearly always the last thing asked.
    focused = questions[-1]
    return focused if len(tokenize(focused)) >= 2 else text


def tokenize(text: str) -> List[str]:
    text = normalize_phrases(text or "")
    tokens = [t for t in _WORD_RE.findall(text.lower()) if t not in _STOPWORDS]
    return tokens


def expand_query(tokens: Iterable[str]) -> List[str]:
    expanded = list(tokens)
    for tok in list(tokens):
        for syn in _SYNONYMS.get(tok, []):
            expanded.extend(_WORD_RE.findall(syn))
    return expanded


class BM25:
    """Standard BM25 Okapi ranking over the chunk corpus."""

    def __init__(self, corpus_tokens: List[List[str]], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.corpus_tokens = corpus_tokens
        self.doc_count = len(corpus_tokens)
        self.doc_lengths = [len(d) for d in corpus_tokens]
        self.avg_doc_length = (sum(self.doc_lengths) / self.doc_count) if self.doc_count else 0.0
        self.term_freqs = [Counter(d) for d in corpus_tokens]
        self.doc_freq: Counter = Counter()
        for tokens in corpus_tokens:
            for term in set(tokens):
                self.doc_freq[term] += 1

    def _idf(self, term: str) -> float:
        n_q = self.doc_freq.get(term, 0)
        if n_q == 0:
            return 0.0
        return math.log((self.doc_count - n_q + 0.5) / (n_q + 0.5) + 1.0)

    def score(self, query_tokens: List[str], index: int) -> float:
        if not self.doc_count or self.avg_doc_length == 0:
            return 0.0
        freqs = self.term_freqs[index]
        doc_len = self.doc_lengths[index]
        total = 0.0
        for term in query_tokens:
            tf = freqs.get(term, 0)
            if tf == 0:
                continue
            idf = self._idf(term)
            denom = tf + self.k1 * (1 - self.b + self.b * doc_len / self.avg_doc_length)
            total += idf * (tf * (self.k1 + 1)) / denom
        return total

    def scores(self, query_tokens: List[str]) -> List[float]:
        return [self.score(query_tokens, i) for i in range(self.doc_count)]


def normalize(values: List[float]) -> List[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return [0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]


def blend(lexical: List[float], dense: List[float], lexical_weight: float = 0.6) -> List[float]:
    """Blends normalised lexical and dense scores into a single ranking score."""
    lex_n = normalize(lexical)
    dense_n = normalize(dense)
    if not lex_n:
        return dense_n
    if not dense_n:
        return lex_n
    return [
        lexical_weight * l + (1 - lexical_weight) * d
        for l, d in zip(lex_n, dense_n)
    ]
