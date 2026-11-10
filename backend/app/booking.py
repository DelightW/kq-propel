"""
Booking lookup: the passenger's own trip, from a booking reference and surname.

Without a booking the assistant answers in general terms ("Economy guests on
journeys to Europe may check..."). With one it knows the route, cabin and
flight, so "how many bags can I take?" and "is my flight delayed?" are answered
for that trip. This is also where the IATA NDC part of objective (i) belongs:
the response shape below follows an NDC order (order, passengers, segments with
cabin and fare brand, baggage per segment) in simplified form.

Two modes, like the flight tool:

  * Live (KQ_BOOKING_API_URL set). The reference and surname are POSTed to
    Kenya Airways' booking service; nothing is invented on failure.
  * Demo (no URL). Two fixed demonstration bookings, labelled as such:
    KQDEMO / Wanjiru and KQBIZ1 / Otieno.

Safeguards, because a booking holds personal data:

  * A booking is found only with the reference AND the surname, and "not found"
    is the same answer whichever of the two was wrong, so neither can be
    confirmed on its own.
  * A session that fails BOOKING_LOOKUP_MAX_FAILURES lookups within an hour is
    refused further lookups, so references cannot be guessed.
  * Only the trip itself (flights, route, cabin, fare, baggage) is remembered,
    for the conversation-memory period. Names are never stored; the surname
    stays only in the passenger's own message, which follows the normal
    retention and erasure rules.

Proposed live request and response (docs/kq_integration_spec.md):

  POST {KQ_BOOKING_API_URL}/orders/retrieve   {"pnr": "ABC12D", "surname": "Wanjiru"}
  200 {"pnr": "ABC12D", "segments": [{"flightNumber": "KQ100",
        "origin": {"iata": "NBO", "city": "Nairobi"},
        "destination": {"iata": "LHR", "city": "London"},
        "departureUtc": "2026-12-12T22:40:00Z", "cabin": "Economy",
        "fareBrand": "Economy Classic",
        "checkedBaggage": {"pieces": 2, "weightKg": 23}}]}
  404 no booking with that reference and surname
"""
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

from app import config, database

SOURCE_LIVE = "kenya_airways_booking"
SOURCE_DEMO = "demo"

_DEMO = {
    ("KQDEMO", "WANJIRU"): [{
        "flight_number": "KQ100", "origin": "Nairobi", "origin_code": "NBO",
        "destination": "London", "destination_code": "LHR", "departure_utc": "2026-12-12T22:40Z",
        "cabin": "Economy", "fare_brand": "Economy Classic",
        "checked_baggage": {"pieces": 2, "weight_kg": 23}}],
    ("KQBIZ1", "OTIENO"): [{
        "flight_number": "KQ203", "origin": "Nairobi", "origin_code": "NBO",
        "destination": "Amsterdam", "destination_code": "AMS", "departure_utc": "2026-12-20T08:15Z",
        "cabin": "Business", "fare_brand": "Business Flex",
        "checked_baggage": {"pieces": 2, "weight_kg": 32}}],
}

# "booking reference ABC12D", "my PNR is abc12d", "ref: KQDEMO". The words
# before the code are required: a bare six-letter word is far more often a word
# than a booking reference.
_PNR_RE = re.compile(
    r"\b(?:booking|reservation|pnr|confirmation)(?:\s+(?:reference|ref|number|code))?"
    r"(?:\s+(?:is|was))?\s*[:#-]?\s*([A-Z0-9]{6})\b"
    r"|\b(?:reference|ref)(?:\s+(?:is|number))?\s*[:#-]?\s*([A-Z0-9]{6})\b",
    re.IGNORECASE)
_SURNAME_RE = re.compile(
    r"\b(?:surname|last\s+name|family\s+name)(?:\s+(?:is|was))?\s*[:-]?\s*([A-Za-z][A-Za-z'-]{1,29})\b"
    r"|\bunder\s+(?:the\s+name\s+(?:of\s+)?)?(?:mr\.?|mrs\.?|ms\.?|dr\.?)?\s*([A-Z][A-Za-z'-]{1,29})\b",
    re.IGNORECASE)
_NOT_A_CODE = {"NUMBER", "PLEASE", "THANKS", "LOOKUP", "STATUS", "SEARCH"}

# Questions whose answer depends on the cabin and route, and so can use the
# booking to fill them in.
TRIP_SENSITIVE_RE = re.compile(
    r"\b(bags?|baggage|luggage|suitcases?|allowance|pieces?|kg|kilos?|check[\s-]?in|excess|overweight|"
    r"hand\s+luggage|cabin\s+bag|carry[\s-]?on)\b", re.IGNORECASE)


def find_reference(message: str) -> Optional[str]:
    """The booking reference in a message, or None. A six-letter word after
    "booking" is usually just a word ("my booking online"), so a code must
    contain a digit or be typed in capitals to count."""
    for m in _PNR_RE.finditer(message or ""):
        typed = m.group(1) or m.group(2) or ""
        code = typed.upper()
        if not code or code in _NOT_A_CODE:
            continue
        if any(ch.isdigit() for ch in typed) or typed.isupper():
            return code
    return None


def find_surname(message: str) -> Optional[str]:
    m = _SURNAME_RE.search(message or "")
    if not m:
        return None
    return (m.group(1) or m.group(2)).strip("'-")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _result(source: str, available: bool, segments: Optional[List[Dict]] = None,
            reason: Optional[str] = None) -> Dict:
    return {"source": source, "is_live": source == SOURCE_LIVE, "available": available,
            "segments": segments or [], "reason": reason,
            "retrieved_at_utc": _now().strftime("%Y-%m-%dT%H:%MZ")}


# ------------------------------------------------------------------ guessing

def failures_in_last_hour(session_id: str) -> int:
    since = (datetime.utcnow() - timedelta(hours=1)).isoformat()
    with database._connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM booking_lookups WHERE session_id = ? AND found = 0 "
                            "AND created_at >= ?", (session_id, since)).fetchone()[0]


def _record(session_id: str, found: bool) -> None:
    with database._connect() as conn:
        conn.execute("INSERT INTO booking_lookups (session_id, found, created_at) VALUES (?,?,?)",
                     (session_id, 1 if found else 0, datetime.utcnow().isoformat()))


# ------------------------------------------------------------------ providers

def normalize(body: Dict) -> List[Dict]:
    """NDC-style order -> the segments the agent uses. Passenger names in the
    response are deliberately not copied."""
    out = []
    for seg in body.get("segments") or []:
        o, d, bag = seg.get("origin") or {}, seg.get("destination") or {}, seg.get("checkedBaggage") or {}
        out.append({
            "flight_number": (seg.get("flightNumber") or "").upper() or None,
            "origin": o.get("city") or o.get("name"), "origin_code": o.get("iata"),
            "destination": d.get("city") or d.get("name"), "destination_code": d.get("iata"),
            "departure_utc": seg.get("departureUtc"),
            "cabin": seg.get("cabin"), "fare_brand": seg.get("fareBrand"),
            "checked_baggage": ({"pieces": bag.get("pieces"), "weight_kg": bag.get("weightKg")}
                                if bag else None),
        })
    return out


def _fetch_live(reference: str, surname: str) -> Optional[List[Dict]]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if config.KQ_BOOKING_API_TOKEN:
        headers["Authorization"] = f"Bearer {config.KQ_BOOKING_API_TOKEN}"
    # POST, not GET: a surname in a URL ends up in proxy and server logs.
    resp = requests.post(f"{config.KQ_BOOKING_API_URL}/orders/retrieve",
                         json={"pnr": reference, "surname": surname}, headers=headers,
                         timeout=config.KQ_API_TIMEOUT_SECONDS)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return normalize(resp.json())


last_error: Optional[str] = None


def lookup(session_id: str, reference: str, surname: str) -> Dict:
    """Finds the booking, or explains why not. Never raises."""
    global last_error
    if failures_in_last_hour(session_id) >= config.BOOKING_LOOKUP_MAX_FAILURES:
        return _result(SOURCE_LIVE if config.KQ_BOOKING_API_URL else SOURCE_DEMO, False,
                       reason="locked")
    reference, key_surname = reference.upper().strip(), surname.upper().strip()
    if not config.KQ_BOOKING_API_URL:
        segments = _DEMO.get((reference, key_surname))
        _record(session_id, bool(segments))
        return _result(SOURCE_DEMO, bool(segments), segments, None if segments else "not_found")
    try:
        segments = _fetch_live(reference, surname.strip())
    except Exception as exc:
        last_error = f"{type(exc).__name__}: {exc}"
        return _result(SOURCE_LIVE, False, reason="unavailable")
    last_error = None
    _record(session_id, bool(segments))
    return _result(SOURCE_LIVE, bool(segments), segments, None if segments else "not_found")


# ------------------------------------------------------------------ wording

def _when(stamp: Optional[str]) -> str:
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).strftime("%d %b %Y, %H:%M UTC")
    except (AttributeError, ValueError):
        return "date not given"


def describe(result: Dict, reference: str) -> str:
    """The passenger-facing reply to a lookup. The reference is shown only
    once a booking has been found with the matching surname."""
    if not result["available"]:
        return {
            "locked": ("For your security I can't look up any more bookings in this conversation "
                       "right now. Please try again later, or use Manage My Booking on "
                       "kenya-airways.com."),
            "unavailable": ("I couldn't reach the Kenya Airways booking system just now. I can still "
                            "answer general policy questions, or you can use Manage My Booking on "
                            "kenya-airways.com."),
        }.get(result.get("reason"),
              "I couldn't find a booking with that reference and surname. Please check both "
              "and try again - the reference is the six-character code on your e-ticket.")
    lines = []
    for s in result["segments"]:
        bag = s.get("checked_baggage") or {}
        bag_text = (f"; checked baggage on this fare: {bag['pieces']} piece(s) of up to {bag['weight_kg']}kg"
                    if bag.get("pieces") and bag.get("weight_kg") else "")
        lines.append(f"- {s.get('flight_number')} {s.get('origin')} ({s.get('origin_code')}) to "
                     f"{s.get('destination')} ({s.get('destination_code')}), {_when(s.get('departure_utc'))}, "
                     f"{s.get('cabin')} ({s.get('fare_brand')}){bag_text}")
    head = (f"I found booking {reference}." if result["is_live"] else
            f"I found demonstration booking {reference} - this is sample data, not a real booking.")
    return (head + "\n\n" + "\n".join(lines) + "\n\n"
            "You can now ask about baggage, check-in or your flight's status, and I'll answer for this trip.")


def remembered(session_id: str) -> List[Dict]:
    from app import memory
    raw = memory.recall(session_id).get("booking")
    try:
        return json.loads(raw) if raw else []
    except ValueError:
        return []


def enrich(question: str, segments: List[Dict]) -> Optional[str]:
    """Adds the booked cabin and route to a baggage or check-in question that
    states neither, so the policy for this trip is retrieved. Returns None
    when the question already says which cabin or route it means."""
    if not segments or not TRIP_SENSITIVE_RE.search(question):
        return None
    from app import memory, places
    if memory._CABIN_RE.search(question) or places.mentioned(question):
        return None
    first = segments[0]
    if not (first.get("cabin") and first.get("origin") and first.get("destination")):
        return None
    stem = question.rstrip(" ?.!")
    if CHECK_IN_RE.search(question) and not BAGGAGE_RE.search(question):
        # Check-in rules differ by journey type, not by cabin.
        if re.search(r"\b(domestic|international)\b", question, re.IGNORECASE):
            return None
        return f"{stem} for {scope(first)} flights from {first['origin']} to {first['destination']}?"
    return f"{stem} in {first['cabin']} from {first['origin']} to {first['destination']}?"


# Airports in Kenya served by Kenya Airways and its regional partner.
_KENYAN_AIRPORTS = {"NBO", "MBA", "KIS", "EDL", "LAU", "MYD", "UKA", "WIL", "LOK"}
CHECK_IN_RE = re.compile(r"\bcheck[\s-]?in\b", re.IGNORECASE)
BAGGAGE_RE = re.compile(r"\b(bags?|baggage|luggage|suitcases?|allowance|pieces?|kg|kilos?|excess|"
                        r"overweight|carry[\s-]?on)\b", re.IGNORECASE)


def scope(segment: Dict) -> str:
    codes = {segment.get("origin_code"), segment.get("destination_code")}
    return "domestic" if codes <= _KENYAN_AIRPORTS else "international"


def allowance_note(question: str, segments: List[Dict]) -> Optional[str]:
    """The checked-baggage allowance the airline's booking system holds for
    this fare, for a baggage question. It is the passenger's own entitlement,
    so it is stated first; the policy text follows for the rules around it."""
    if not segments or not BAGGAGE_RE.search(question):
        return None
    first = segments[0]
    bag = first.get("checked_baggage") or {}
    if not (bag.get("pieces") and bag.get("weight_kg")):
        return None
    return (f"Your booking on {first.get('flight_number')} ({first.get('cabin')}, "
            f"{first.get('fare_brand')} fare) includes {bag['pieces']} checked piece(s) of up to "
            f"{bag['weight_kg']}kg each, according to the booking record.")
