"""
Paying for an extra checked bag on a looked-up booking.

Kenya Airways charges a flat heavy-bag fee per additional or overweight piece,
set by region and by whether the journey begins at the Nairobi hub (Excess
Baggage Fees policy, Section 2). The fee therefore depends on the trip, so it
is only quoted once a booking has been looked up; without one the passenger is
asked for it.

The published table carries no currency label. The same page states its
administrative fees in US dollars, so this prototype charges the heavy-bag fee
in US dollars and says so in the reply every time; the general fee path
(agent._payable_fee) still never charges an unlabelled figure.

The amount always comes from the policy text and the booked route, never from
the passenger's message. One payment covers one piece.
"""
import re
from typing import Dict, List, Optional

from app import booking, places

CURRENCY = "USD"
SEARCH = "heavy bag fees by region journeys beginning at the Nairobi hub"

WANT_RE = re.compile(
    r"\b(pay|paying|buy|buying|purchase|add|adding|book|prepay|want|need)\b.{0,30}?"
    r"\b(extra|additional|added|add-on|excess|another|second|third|more|heavy|heavier|overweight|"
    r"oversized?)\s+(checked\s+)?(bags?|baggage|luggage|suitcases?|pieces?|kg|kilos?|weight)\b"
    r"|\b(pay|paying|buy|buying|purchase|add|adding|book|prepay|want|need)\b.{0,30}?"
    r"\b(bags?|baggage|luggage|suitcases?)\s+(that\s+is\s+|which\s+is\s+)?"
    r"(over\s*weight|over\s+the\s+limit|too\s+heavy|extra)\b",
    re.IGNORECASE)
# "add 1 bag", "buy two more bags", "pay for another suitcase".
_ADD_RE = re.compile(
    r"\b(add|adding|buy|buying|purchase|pay\s+for)\s+(?:(?:a|an|one|two|three|\d+|another|my)\s+)?"
    r"(?:more\s+|extra\s+)?(?:checked\s+)?(bags?|baggage|luggage|suitcases?|pieces?)\b",
    re.IGNORECASE)
# "how much is an extra bag?": a price question. Answered with an offer only
# once a booking gives the route; without one it is a policy question.
_PRICE_RE = re.compile(r"\b(how\s+much|cost|costs|price|fee|charge)\b", re.IGNORECASE)
_MORE_BAGS_RE = re.compile(
    r"\b(extra|additional|added|another|more|heavy|heavier|overweight|excess|second|third|\d+)\s+"
    r"(?:checked\s+)?(bags?|baggage|luggage|suitcases?|pieces?|kg|kilos?|weight)\b", re.IGNORECASE)
# A question about an allowance ("do I get an extra bag as a Gold member?") is
# about something free, not something to pay for.
_ENTITLEMENT_RE = re.compile(r"\b(free|entitled|allowance|allowed|do\s+i\s+get|asante|member)\b",
                             re.IGNORECASE)

_ROW_RE = re.compile(r"Journeys\s+beginning\s+at\s+(the\s+Nairobi\s+hub|all\s+other\s+points)\s+"
                     r"are\s+charged\s+(.*?)\.", re.IGNORECASE)
_ITEM_RE = re.compile(r"(\d+(?:\.\d+)?)\s+(?:for|within|on)\s+(?:the\s+)?"
                      r"(Europe|Americas|Asia|Middle\s+East|Africa|domestic\s+services)", re.IGNORECASE)
_KEYS = {"europe": places.EUROPE, "americas": places.AMERICAS, "asia": places.ASIA,
         "middle east": places.MIDDLE_EAST, "africa": places.AFRICA, "domestic services": "domestic"}


def wants_extra_bag(message: str, has_booking: bool = False) -> bool:
    message = message or ""
    if _ENTITLEMENT_RE.search(message):
        return False
    if WANT_RE.search(message) or _ADD_RE.search(message):
        return True
    # A price question naming a place may be about another route than the
    # booked one, so it is left to the policy answer.
    return (has_booking and bool(_PRICE_RE.search(message) and _MORE_BAGS_RE.search(message))
            and not places.mentioned(message))


def fees(chunks: List[Dict]) -> Optional[Dict]:
    """{"hub": {region: amount}, "other": {...}, "source": "..."} as the policy
    states them, or None when no retrieved section states the table."""
    for chunk in chunks:
        text = re.sub(r"\s+", " ", chunk.get("text") or "")
        rows = {}
        for which, items in _ROW_RE.findall(text):
            key = "hub" if "nairobi" in which.lower() else "other"
            rows[key] = {_KEYS[re.sub(r"\s+", " ", region.lower())]: float(amount)
                         for amount, region in _ITEM_RE.findall(items)}
        if rows.get("hub") and rows.get("other"):
            rows["source"] = f"{chunk.get('doc_title') or chunk.get('source')} - {chunk.get('section')}"
            return rows
    return None


def _region(city: Optional[str]) -> Optional[str]:
    if not city:
        return None
    for region in (places.EUROPE, places.AMERICAS, places.ASIA, places.MIDDLE_EAST, places.AFRICA):
        if region in places.with_parents(city.lower()):
            return region
    return None


def quote(table: Dict, segment: Dict) -> Optional[Dict]:
    """The fee for one extra piece on this segment, with how it was chosen, or
    None when the route's region is not known."""
    from_hub = (segment.get("origin_code") or "").upper() == "NBO"
    row = table["hub" if from_hub else "other"]
    if booking.scope(segment) == "domestic":
        region = "domestic"
    else:
        # The region travelled to; for a journey into Nairobi, the region it starts from.
        far_end = segment.get("origin") if (segment.get("destination_code") or "").upper() == "NBO" \
            else segment.get("destination")
        region = _region(far_end)
    if region not in row:
        return None
    label = {"domestic": "domestic services", places.MIDDLE_EAST: "the Middle East",
             places.AMERICAS: "the Americas", places.AFRICA: "travel within Africa"}.get(region, region.title())
    return {"amount": row[region], "currency": CURRENCY, "region": label, "from_hub": from_hub,
            "source": table["source"]}


def reply(reference: str, segment: Dict, q: Dict) -> str:
    start = "begins at the Nairobi hub" if q["from_hub"] else "does not begin at the Nairobi hub"
    return (f"Your booking **{reference}** is {segment.get('flight_number')} from {segment.get('origin')} to "
            f"{segment.get('destination')}. The journey {start}, so the heavy bag fee for "
            f"{q['region']} applies: **{q['amount']:,.0f}** per additional or overweight piece "
            f"({q['source']}).\n\n"
            f"Kenya Airways publishes this table without a currency label; the other fees on the same "
            f"page are in US dollars, so this prototype charges it as **{CURRENCY} {q['amount']:,.2f}**. "
            f"This covers one piece; for more pieces, pay for each one separately.")


def ask_for_booking() -> str:
    from app import config
    text = ("The extra bag fee depends on your route, so I need your booking first. Please send your "
            "**booking reference** and **surname**, for example \"booking ABC123, surname Kanja\", and "
            "I'll quote the fee for that trip.")
    if not config.KQ_BOOKING_API_URL:
        text += ("\n\nThis prototype is not connected to Kenya Airways' booking system, so only demonstration "
                 "bookings can be found. To try one, use booking **KQDEMO** with surname **Wanjiru** "
                 "(sample data, not a real booking).")
    return text
