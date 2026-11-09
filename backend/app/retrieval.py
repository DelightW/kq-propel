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
from functools import lru_cache
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
    "latest": ["close", "closes", "closing", "counters", "checkin", "before"],
    "compensation": ["compensation", "entitled", "voucher"],
    "pay": ["payment", "pay", "mpesa"],
    "mpesa": ["m-pesa", "mpesa", "payment", "mobile"],
    "refund": ["refund", "refundable"],
    "checkin": ["check-in", "checkin"],
    "check": ["checked", "allowance"],
    "lost": ["lost", "delayed", "missing", "traced"],
    "missing": ["lost", "traced", "worldtracer", "file"],
    # Kenya Airways does not use the industry phrase "Property Irregularity
    # Report"; it issues a "file reference number". Guests ask using the
    # industry phrase, so it is aliased onto the airline's own vocabulary.
    "irregularity": ["file", "reference", "worldtracer", "tracing"],
    "pir": ["file", "reference", "worldtracer", "tracing"],
    "property": ["file", "reference", "tracing"],
    "cabin": ["hand", "cabin", "carry-on"],
    "carry": ["hand", "cabin"],
    "hand": ["cabin", "hand"],
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
    # sections that state processing timelines. "take" on its own is the
    # ordinary verb ("can I take my bicycle?") and must not be expanded, or
    # every carriage question is pulled toward refund processing times; the
    # duration reading is recognised as a phrase instead.
    "long": ["processing", "processed", "timelines", "within", "business", "days"],
    "duration": ["processing", "timelines", "within"],
    "timeline": ["timelines", "processing", "within"],
    "timelines": ["processing", "within"],
    "soon": ["processing", "within", "days"],
    # Passengers describe a name correction in their own words ("my surname is
    # spelled wrong by two letters"); the policy says "name correction" and
    # "characters". Without these the question shared no subject word with
    # the section that answers it.
    "surname": ["name"],
    "spelled": ["correction", "name"],
    "spelling": ["correction", "name"],
    "misspelled": ["correction", "name"],
    "typo": ["correction", "name"],
    "letter": ["character"],
    "fix": ["correction"],
    "wrong": ["correction"],
}

# Multi-word phrases are collapsed *before* tokenisation, so a phrasal verb is
# never mistaken for its component words. Critically, "take off" means depart -
# it must not be expanded through the "take" -> processing-time synonyms used
# for questions like "how long does a refund take?".
_PHRASE_REWRITES = [
    (re.compile(r"\btak(?:e|es|ing)[\s-]?off\b", re.IGNORECASE), "departure"),
    (re.compile(r"\btake[\s-]?off\b", re.IGNORECASE), "departure"),
    # "check in" is the airport process, but "how many bags can I check in
    # Economy" uses "check" as a plain verb with "in Economy" as its adjunct.
    # Gluing that into "checkin" sent every checked-baggage question to the
    # check-in policy. Hyphenated and closed forms are unambiguous; the spaced
    # form is only rewritten when a cabin or quantity noun does not follow.
    (re.compile(r"\bcheck[-]in\b", re.IGNORECASE), "checkin"),
    (re.compile(r"\bcheck\s+in\b(?!\s+(?:economy|business|first|premier|"
                r"cabin|coach|total|the\s+hold))", re.IGNORECASE), "checkin"),
    (re.compile(r"\bm[\s-]?pesa\b", re.IGNORECASE), "mpesa"),
    (re.compile(r"\bcash\s+back\b", re.IGNORECASE), "refund"),
    (re.compile(r"\bcarry[\s-]?on\b", re.IGNORECASE), "cabin"),
    (re.compile(r"\bno[\s-]?show\b", re.IGNORECASE), "noshow"),
    # "how late can I arrive" asks about counter closing times, not about a
    # delayed flight, so it must not reach the "late" -> delay synonyms.
    (re.compile(r"\bhow\s+late\b", re.IGNORECASE), "latest"),
    # Only the duration reading of "take" maps to processing timelines.
    (re.compile(r"\b(?:how\s+long\s+)?(?:does|do|will|to)\s+"
                r"(?:\w+\s+){0,3}?tak(?:e|es)\b(?=\s*(?:\?|to\b|for\b|$))",
                re.IGNORECASE), "processing timelines within"),
]


def normalize_phrases(text: str) -> str:
    for pattern, replacement in _PHRASE_REWRITES:
        text = pattern.sub(replacement, text)
    return text


_INTERROGATIVE_RE = re.compile(
    r"\b(what|where|when|why|which|who|whose|how|can|could|may|might|will|would|"
    r"shall|should|do|does|did|is|are|was|were|am|have|has|had|please|tell\s+me|"
    r"i\s+need|i\s+want|help\s+me)\b", re.IGNORECASE)


def _is_emotional(clause: str) -> bool:
    """True when a clause is venting rather than carrying request detail.

    Imported lazily: retrieval is used by tooling that must not pay the cost of
    loading the classifier, and only multi-clause messages reach this path.
    """
    try:
        from app import sentiment
    except Exception:
        return False
    try:
        result = sentiment.classify_frustration(clause)
    except Exception:
        return False
    return result.get("label") == "frustrated" and \
        float(result.get("frustration_score") or 0.0) >= 0.55


def focus_query(text: str) -> str:
    """Narrows a message to the clause that actually carries the request.

    Passengers frequently prefix a question with an emotional statement - "I
    HAVE WAITED FOR 2 HOURS?! What is the baggage allowance?" - and those
    words are real tokens that pull retrieval toward delay and compensation
    sections. The emotional prefix is what the frustration classifier is for;
    retrieval should see only the request itself.

    Not every preceding clause is emotional, though. "I bought my ticket from a
    travel agent. Can you refund me directly?" carries its subject in the first
    clause, and discarding it sent the question to whichever refund section
    happened to rank first. The frustration classifier - the component that
    already exists to tell venting from content - decides which clauses are
    dropped, rather than position alone.
    """
    text = (text or "").strip()
    clauses = [c.strip() for c in re.split(r"(?<=[.!?])\s+|\n+", text) if c.strip()]
    if len(clauses) < 2:
        return text

    question_idx = [i for i, c in enumerate(clauses) if _INTERROGATIVE_RE.search(c)]
    if not question_idx:
        return text
    # The request is nearly always the last thing asked.
    focused_i = question_idx[-1]
    focused = clauses[focused_i]
    if len(tokenize(focused)) < 2:
        return text

    kept = [c for i, c in enumerate(clauses)
            if i == focused_i or not _is_emotional(c)]
    return " ".join(kept) if len(kept) > 1 else focused


_PLURAL_KEEP = {"business", "address", "excess", "class", "gas", "always",
                "plus", "bonus", "status", "miss", "press", "less", "this",
                "as", "is", "was", "has", "us", "its"}

# Splits a compound *request* into the separate things being asked. Applied
# only to the interrogative part of a message: an earlier version split the
# whole text and cut declarative context clauses like "My bag is delayed" or
# "I am travelling with an infant and a stroller" into useless fragments,
# which cost 0.033 recall.
_SUBQUERY_SPLIT_RE = re.compile(
    r"\s+(?:and|also|plus|as\s+well\s+as)\s+|\s*;\s*", re.IGNORECASE)
# A fragment only counts as its own request if it opens with an interrogative.
_WH_OPENER_RE = re.compile(
    r"^\s*(what|when|where|which|who|why|how|can|could|do|does|did|is|are|"
    r"will|would|should|am|must|may)\b", re.IGNORECASE)


def subqueries(text: str, max_parts: int = 3) -> List[str]:
    """Split a compound question into the separate requests it contains.

    A question like "What am I owed and what do I owe?" carries two intents.
    Embedded as one vector it lands between them and matches neither well: on
    the evaluation set the chunk answering the second half ranked #20 of 52.
    Ranking each intent separately and fusing the scores puts both in context.

    Only the interrogative sentence is split, and each fragment keeps the
    surrounding declarative context - "...one piece over the allowance to
    Europe" belongs to both halves of the question that follows it, and a
    fragment stripped of it retrieves nothing useful. A fragment must open with
    an interrogative word to count, so lists ("an infant and a stroller") are
    left alone.

    Returns a single-element list when the question is not compound. Callers
    must treat that as the normal case: most questions are not compound, and a
    split that fires too eagerly costs recall on the easy majority.
    """
    text = (text or "").strip()
    if not text:
        return []

    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]
    if not sentences:
        return [text]

    # The request is the last interrogative sentence; everything else is the
    # context that every sub-question needs to keep.
    q_idx = [i for i, s in enumerate(sentences) if _INTERROGATIVE_RE.search(s)]
    if not q_idx:
        return [text]
    target = q_idx[-1]
    question = sentences[target]
    context = " ".join(sentences[:target]).strip()

    raw = [p.strip(" ,.?!") for p in _SUBQUERY_SPLIT_RE.split(question) if p and p.strip()]
    if len(raw) < 2:
        return [text]

    fragments = [p for p in raw if _WH_OPENER_RE.match(p) and len(tokenize(p)) >= 2]
    if len(fragments) < 2:
        return [text]

    # The whole question stays first: it is still the single best query for
    # most of the corpus, and fusion only ever promotes a chunk above it.
    out = [text]
    for frag in fragments[:max_parts]:
        combined = (context + " " + frag).strip() if context else frag
        if combined.lower() != text.lower():
            out.append(combined)
    return out


def fold_plural(token: str) -> str:
    """Conservative singular/plural folding for lexical matching.

    BM25 treats "surfboard" and "surfboards" as unrelated terms, so a question
    about a surfboard scored 0.000 against the only chunk that mentions one -
    the chunk holding the answer ranked #14 of 52 on the dense signal alone.
    A full stemmer is more aggressive than this corpus wants (it would fold
    "business" to "busi" and risk mangling fare-class words), so this handles
    only regular plurals and leaves anything doubtful alone.

    Applied to the corpus and the query through the same `tokenize`, so the two
    sides can never drift apart.
    """
    if len(token) <= 3 or token.isdigit() or token in _PLURAL_KEEP:
        return token
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith(("sses", "shes", "ches", "xes", "zes")):
        return token[:-2]
    if token.endswith("ss"):
        return token
    if token.endswith("s"):
        return token[:-1]
    return token


def tokenize(text: str) -> List[str]:
    text = normalize_phrases(text or "")
    tokens = [t for t in _WORD_RE.findall(text.lower()) if t not in _STOPWORDS]
    return [fold_plural(t) for t in tokens]


# Synonym keys are written naturally, so four of them ("bags", "documents",
# "sports", "timelines") are plural and would have stopped matching the moment
# tokenize started folding plurals - silently, with no error and no test
# failure. Folding the keys once here keeps the two sides from drifting.
_SYNONYMS_FOLDED = {}
for _key, _vals in _SYNONYMS.items():
    _SYNONYMS_FOLDED.setdefault(fold_plural(_key), []).extend(_vals)


def _synonyms_for(token: str) -> List[str]:
    return _SYNONYMS_FOLDED.get(token, [])


def expand_query(tokens: Iterable[str]) -> List[str]:
    expanded = list(tokens)
    for tok in list(tokens):
        for syn in _synonyms_for(tok):
            expanded.extend(fold_plural(t) for t in _WORD_RE.findall(syn))
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


# Words that carry no subject matter in a passenger question. They are excluded
# when the grounding guard decides whether retrieved policy text is on topic:
# "My bag has been missing and NOBODY is helping me" is about a missing bag,
# and counting "been", "nobody" and "helping" as unmatched topic words was
# enough to make the guard refuse a question the policy answers.
_GENERIC_QUERY_WORDS = frozenset("""
where which whose whom there here any some about been being get got gets give
tell know need want like please help helping helped nobody anybody anyone
someone somebody everyone thing things just also very really still yet again
much many lot lots would could should shall may might must let make made
going go goes went come came say said told ask asked right now today
tomorrow yesterday time thank thanks hello hi hey okay ok sure yes no
by into onto than then them they their these those we us all each every other
such only even non way
""".split())


@lru_cache(maxsize=1)
def corpus_vocabulary() -> frozenset:
    """Every token in the policy corpus, tokenised exactly as questions are.

    Read from the source documents rather than the vector store so the guard
    behaves the same whichever store back end is active.
    """
    from app import config
    vocab = set()
    for path in sorted(config.POLICY_DIR.glob("*.txt")):
        vocab.update(tokenize(path.read_text(encoding="utf-8")))
    return frozenset(vocab)


def topical_tokens(text: str) -> List[str]:
    """Question tokens that say what is being asked about.

    Drops generic words and place names. A place says where the passenger is
    travelling, not what they want to know, so it cannot on its own show that
    a policy section answers the question.
    """
    from app import places
    return [t for t in tokenize(text)
            if t not in _GENERIC_QUERY_WORDS and t not in places.PLACE_WORDS]
