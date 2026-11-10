"""
Conversation memory: what the last answered question was about.

Before this, only a pending payment survived from one message to the next, so
a passenger who asked about Economy baggage to London and then wrote "what
about Business class?" got an answer to "what about Business class?" alone.

Two things are remembered per session, for CONVERSATION_MEMORY_MINUTES:

  last_query     the last question the agent answered from policy, after any
                 follow-up had been resolved into it
  flight_number  the last flight the passenger named

A follow-up is rewritten into a complete question before retrieval. The details
the follow-up changes (cabin, destination, origin) replace the ones in the
earlier question; everything else is kept:

  "How many bags can I check in Economy from Nairobi to London?"
  + "What about Business class?"
  = "How many bags can I check in from Nairobi to London? Business class"

The rewritten question is shown in the agent's trace, so staff can see when an
answer relied on an earlier turn.
"""
import re
from datetime import datetime, timedelta
from typing import Dict, Optional

from app import config, database, places

# Openers that only make sense as a continuation of the previous question.
_FOLLOW_UP_RE = re.compile(
    r"^\s*(?:and|but|also|so|ok(?:ay)?,?\s+(?:and|what\s+about)|what\s+about|how\s+about|"
    r"what\s+if|same\s+(?:for|with|question|thing)|and\s+(?:for|if|in|on|to|with)|"
    r"is\s+(?:that|it)\s+the\s+same|does\s+(?:that|this|it)\s+(?:also\s+)?apply)\b",
    re.IGNORECASE)
_LEADING_MARKER_RE = re.compile(
    r"^\s*(?:and|but|also|so|ok(?:ay)?,?|what\s+about|how\s+about|what\s+if|"
    r"same\s+(?:for|with|question|thing)(?:\s+for)?)\s*[,:]?\s*", re.IGNORECASE)

_CABIN_RE = re.compile(
    r"\b(?:premium\s+economy|economy|business|first)(?:\s+class)?\b|\b(?:class)\b",
    re.IGNORECASE)
# The kind of journey: "what about domestic flights?" replaces "international".
_SCOPE_RE = re.compile(r"\b(?:domestic|international|regional|long[\s-]?haul|short[\s-]?haul)\b",
                       re.IGNORECASE)
# A preposition in front of a place says which end of the journey it is.
_ORIGIN_PREP = {"from"}

# Message words that carry no subject of their own. A message made only of
# these plus a cabin or a place ("Business class?", "And to Dubai?") is a
# follow-up even without an opening marker.
_SLOT_ONLY_FILLER = frozenset("""
and but also so what about how if for to from in on with the a an my class please
same then flight flights flying travelling traveling
""".split())

# A reference to the flight already being discussed.
_FLIGHT_REF_RE = re.compile(
    r"\b(?:that|this|the\s+same|same|my|our|the)\s+flight\b"
    r"|\b(?:is|was|has)\s+it\s+(?:been\s+)?(?:delayed|late|on\s+time|cancell?ed|boarding|"
    r"departed|landed|leaving)\b"
    r"|\bwhich\s+gate\b|\bwhat\s+gate\b|\bflight\s+status\b",
    re.IGNORECASE)


def _now() -> datetime:
    return datetime.utcnow()


def recall(session_id: str) -> Dict:
    """The remembered context for a session, or {} once it has gone stale."""
    with database._connect() as conn:
        row = conn.execute("SELECT * FROM conversation_context WHERE session_id = ?",
                           (session_id,)).fetchone()
    if not row:
        return {}
    row = dict(row)
    try:
        updated = datetime.fromisoformat(row["updated_at"])
    except (TypeError, ValueError):
        return {}
    if _now() - updated > timedelta(minutes=config.CONVERSATION_MEMORY_MINUTES):
        return {}
    return row


def remember(session_id: str, *, query: Optional[str] = None,
             flight_number: Optional[str] = None) -> None:
    """Records the latest answered question and/or flight. A value left as
    None keeps what was stored before."""
    if query is None and flight_number is None:
        return
    with database._connect() as conn:
        conn.execute(
            "INSERT INTO conversation_context (session_id, last_query, flight_number, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
            "last_query = COALESCE(excluded.last_query, conversation_context.last_query), "
            "flight_number = COALESCE(excluded.flight_number, conversation_context.flight_number), "
            "updated_at = excluded.updated_at",
            (session_id, query, flight_number, _now().isoformat()))


def forget(session_id: str) -> None:
    with database._connect() as conn:
        conn.execute("DELETE FROM conversation_context WHERE session_id = ?", (session_id,))


def _place_spans(text: str):
    """(start, end, preposition) for every place word in `text`."""
    clean = places._NOT_A_PLACE_RE.sub(lambda m: " " * len(m.group(0)), text)
    spans = []
    for pattern in (places._PLACE_RE, places._CODE_RE):
        for m in pattern.finditer(clean):
            before = re.findall(r"[a-z]+", clean[:m.start()].lower())
            spans.append((m.start(), m.end(), before[-1] if before else ""))
    return spans


def _without_spans(text: str, spans) -> str:
    for start, end, _ in sorted(spans, reverse=True):
        # Take the preposition with the place, so "from Nairobi to" does not
        # survive as "from Nairobi to ?".
        head = re.sub(r"\b(?:to|for|from|in|at|into)\s+$", "", text[:start], flags=re.IGNORECASE)
        text = head + text[end:]
    text = re.sub(r"\s+([?.!,])", r"\1", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def is_follow_up(message: str) -> bool:
    """Whether a message continues the previous question rather than starting
    a new one."""
    if _FOLLOW_UP_RE.search(message):
        return True
    words = re.findall(r"[a-z]+", message.lower())
    if not words or len(words) > 6:
        return False
    has_slot = bool(_CABIN_RE.search(message) or _SCOPE_RE.search(message)
                    or places.mentioned(message))
    leftover = [w for w in words
                if w not in _SLOT_ONLY_FILLER and w not in places.PLACE_WORDS
                and not _CABIN_RE.fullmatch(w) and not _SCOPE_RE.fullmatch(w)]
    return has_slot and not leftover


def merge(previous: str, follow_up: str) -> str:
    """The previous question with the follow-up's details substituted in."""
    detail = _LEADING_MARKER_RE.sub("", follow_up).strip()
    merged = previous

    # A cabin or journey type is swapped in where the old one stood. Deleting
    # "Economy" from "how many bags can I check in Economy from Nairobi..."
    # and appending "Business class" left "check in from Nairobi", which
    # retrieval read as a check-in question.
    for slot in (_CABIN_RE, _SCOPE_RE):
        new = slot.search(detail)
        if new and slot.search(merged):
            merged = slot.sub(new.group(0), merged, count=1)
            merged = merged[:slot.search(merged).end()] + slot.sub("", merged[slot.search(merged).end():])
            detail = slot.sub("", detail)
    merged = re.sub(r"\s{2,}", " ", merged).strip()
    detail = re.sub(r"\s{2,}", " ", detail).strip(" ,")
    if all(w in _SLOT_ONLY_FILLER for w in re.findall(r"[a-z]+", detail.lower())):
        detail = ""  # nothing left but "flights", "please"...

    new_spans = _place_spans(detail)
    if new_spans and new_spans[0][0] == 0:
        # "Same for Paris" names a destination without saying so.
        detail = "to " + detail
        new_spans = _place_spans(detail)
    if new_spans:
        new_origin = any(prep in _ORIGIN_PREP for _, _, prep in new_spans)
        new_other = any(prep not in _ORIGIN_PREP for _, _, prep in new_spans)
        drop = [s for s in _place_spans(merged)
                if (s[2] in _ORIGIN_PREP and new_origin)
                or (s[2] not in _ORIGIN_PREP and new_other)]
        merged = _without_spans(merged, drop)

    # One sentence, not two: a "?" left in the middle made the request
    # focusing step keep only the follow-up ("to Mumbai?").
    merged = merged.rstrip(" ?.!")
    detail = detail.rstrip(" ?.!")
    return " ".join(p for p in (merged, detail) if p) + "?"


def resolve_query(session_id: str, message: str) -> Optional[str]:
    """The complete question a follow-up stands for, or None when the message
    is not a follow-up or nothing is remembered to follow up on."""
    previous = recall(session_id).get("last_query")
    if not previous or not is_follow_up(message):
        return None
    return merge(previous, message)


def resolve_flight(session_id: str, message: str) -> Optional[str]:
    """The remembered flight when the message refers to "that flight"."""
    if not _FLIGHT_REF_RE.search(message):
        return None
    return recall(session_id).get("flight_number")
