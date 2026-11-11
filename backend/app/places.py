"""
Place names, and the regions they belong to.

Kenya Airways states most baggage rules by region ("between Africa and Europe")
and lists exceptions by named route ("Nairobi to Mumbai"). Passengers ask by
city ("Nairobi to London"). Two components need to reason about that gap:

  * Ranking. A section that lists specific routes, none of which involve a place
    the passenger named, is an exception that does not apply to them. Without
    this, the one-piece exceptions section ranked first for a Nairobi to London
    question purely because it mentions Nairobi.

  * The grounding guard. A place name says where the passenger is travelling,
    not what they are asking about. "What is the weather in Nairobi?" matched
    the baggage policy on "Nairobi" alone and was answered from it.

Each place maps to its parents (country, then region), so "London" is covered by
a section that says "Europe".
"""
import re
from functools import lru_cache
from typing import Dict, FrozenSet, Iterable, Set, Tuple

AFRICA, EUROPE, ASIA, AMERICAS, MIDDLE_EAST = "africa", "europe", "asia", "americas", "middle east"

# place -> parents. Only regions and countries used by the policy corpus are
# needed as parents; every city must reach one of them.
_PARENTS: Dict[str, Tuple[str, ...]] = {
    # Regions and countries
    "africa": (), "europe": (), "asia": (), "americas": (), "middle east": (),
    "kenya": (AFRICA,), "tanzania": (AFRICA,), "uganda": (AFRICA,), "rwanda": (AFRICA,),
    "ethiopia": (AFRICA,), "nigeria": (AFRICA,), "ghana": (AFRICA,), "south africa": (AFRICA,),
    "zambia": (AFRICA,), "zimbabwe": (AFRICA,), "egypt": (AFRICA,), "congo": (AFRICA,),
    "united kingdom": (EUROPE,), "france": (EUROPE,), "netherlands": (EUROPE,), "italy": (EUROPE,),
    "switzerland": (EUROPE,), "germany": (EUROPE,),
    "united states": (AMERICAS,), "canada": (AMERICAS,),
    "india": (ASIA,), "china": (ASIA,), "thailand": (ASIA,),
    "united arab emirates": (MIDDLE_EAST,), "saudi arabia": (MIDDLE_EAST,), "oman": (MIDDLE_EAST,),
    # Kenya
    "nairobi": ("kenya", AFRICA), "mombasa": ("kenya", AFRICA), "kisumu": ("kenya", AFRICA),
    "malindi": ("kenya", AFRICA), "eldoret": ("kenya", AFRICA), "lamu": ("kenya", AFRICA),
    "lodwar": ("kenya", AFRICA), "ukunda": ("kenya", AFRICA), "diani": ("kenya", AFRICA),
    # Rest of Africa
    "dar es salaam": ("tanzania", AFRICA), "zanzibar": ("tanzania", AFRICA),
    "kilimanjaro": ("tanzania", AFRICA), "entebbe": ("uganda", AFRICA), "kampala": ("uganda", AFRICA),
    "kigali": ("rwanda", AFRICA), "addis ababa": ("ethiopia", AFRICA), "lagos": ("nigeria", AFRICA),
    "accra": ("ghana", AFRICA), "johannesburg": ("south africa", AFRICA),
    "cape town": ("south africa", AFRICA), "lusaka": ("zambia", AFRICA),
    "harare": ("zimbabwe", AFRICA), "cairo": ("egypt", AFRICA), "kinshasa": ("congo", AFRICA),
    "juba": (AFRICA,), "mogadishu": (AFRICA,), "mauritius": (AFRICA,), "seychelles": (AFRICA,),
    # Europe
    "london": ("united kingdom", EUROPE), "paris": ("france", EUROPE),
    "amsterdam": ("netherlands", EUROPE), "rome": ("italy", EUROPE),
    "geneva": ("switzerland", EUROPE), "frankfurt": ("germany", EUROPE),
    # Americas
    "new york": ("united states", AMERICAS),
    # Middle East
    "dubai": ("united arab emirates", MIDDLE_EAST), "jeddah": ("saudi arabia", MIDDLE_EAST),
    "muscat": ("oman", MIDDLE_EAST),
    # Asia
    "mumbai": ("india", ASIA), "delhi": ("india", ASIA), "guangzhou": ("china", ASIA),
    "bangkok": ("thailand", ASIA), "hong kong": ("china", ASIA),
}

# Other spellings that should resolve to a canonical place.
_ALIASES = {
    "kenyan": "kenya", "uk": "united kingdom", "britain": "united kingdom",
    "england": "united kingdom", "usa": "united states", "us": "united states",
    "america": "united states", "uae": "united arab emirates", "holland": "netherlands",
    "dar": "dar es salaam", "jnb": "johannesburg", "nbo": "nairobi", "mba": "mombasa",
    "lhr": "london", "cdg": "paris", "ams": "amsterdam", "jfk": "new york",
    "dxb": "dubai", "bom": "mumbai", "can": "guangzhou", "bkk": "bangkok",
    "eu": "europe",
}

# Every surface form, longest first so "dar es salaam" wins over "dar".
_FORMS = sorted(set(_PARENTS) | set(_ALIASES), key=len, reverse=True)
# Three-letter airport codes are only treated as places in upper case
# ("NBO"), so the English words "can" and "us" are never read as Guangzhou or
# the United States.
#
# "us" is never a place here, in either case: the corpus states fees in
# "US dollars", and reading that as the United States would make every fee
# section look route-specific.
_CODE_FORMS = {f for f in _ALIASES if len(f) in (2, 3) and f.isalpha() and f not in ("usa", "uae", "us", "uk")}
_WORD_FORMS = [f for f in _FORMS if f not in _CODE_FORMS and f != "us"]
_PLACE_RE = re.compile(r"\b(" + "|".join(re.escape(f) for f in _WORD_FORMS) + r")\b", re.IGNORECASE)
_CODE_RE = re.compile(r"\b(" + "|".join(sorted(f.upper() for f in _CODE_FORMS)) + r")\b")

# Phrases that contain a place name without being about that place. The
# airline's own name appears in almost every section, and "Kenyan shillings"
# is a currency; leaving either in would mark nearly every chunk as being
# about Kenya.
_NOT_A_PLACE_RE = re.compile(
    r"\bkenya\s+airways\b|\bkenyan?\s+shillings?\b|\bunited\s+states\s+dollars?\b",
    re.IGNORECASE)

# Every single word that belongs to a place name, for removing place words
# from a tokenised question.
PLACE_WORDS: FrozenSet[str] = frozenset(
    w for f in _FORMS for w in re.findall(r"[a-z0-9]+", f) if f not in _CODE_FORMS and f != "us"
)


def _canonical(form: str) -> str:
    form = form.lower()
    return _ALIASES.get(form, form)


@lru_cache(maxsize=4096)
def mentioned(text: str) -> FrozenSet[str]:
    """Canonical places named in `text`, without their parents."""
    text = _NOT_A_PLACE_RE.sub(" ", text or "")
    found = {_canonical(m) for m in _PLACE_RE.findall(text)}
    found |= {_canonical(m) for m in _CODE_RE.findall(text)}
    return frozenset(found)


def with_parents(place: str) -> Set[str]:
    return {place, *_PARENTS.get(place, ())}


def covered(query_place: str, text_places: Iterable[str]) -> bool:
    """True when a text naming `text_places` speaks about `query_place`,
    either by naming it or by naming its country or region."""
    return bool(with_parents(query_place) & set(text_places))


_REGIONS = frozenset(p for p, parents in _PARENTS.items() if not parents)
_COUNTRIES = frozenset({
    "kenya", "tanzania", "uganda", "rwanda", "ethiopia", "nigeria", "ghana", "south africa", "zambia",
    "zimbabwe", "egypt", "congo", "united kingdom", "france", "netherlands", "italy", "switzerland",
    "germany", "united states", "canada", "india", "china", "thailand", "united arab emirates",
    "saudi arabia", "oman",
})


def is_city(place: str) -> bool:
    return place not in _REGIONS and place not in _COUNTRIES


def route_mismatch(query_text: str, chunk_text: str) -> bool:
    """True when the chunk is a list of specific routes and at least one place
    the passenger named is not covered by it, even at country or region level.

    Only chunks naming two or more cities count as route lists. Sections that
    name countries or regions ("M-Pesa is accepted in Kenya and Tanzania",
    "between Africa and Europe") state where a rule applies, and are exactly
    what a passenger asking about another place needs to read, so they are
    never treated as mismatches.
    """
    asked = mentioned(query_text)
    if not asked:
        return False
    stated = mentioned(chunk_text)
    if sum(1 for p in stated if is_city(p)) < 2:
        return False
    return any(not covered(p, stated) for p in asked)


_ROUTE_WORDS_RE = re.compile(r"\b(routes?|routings?|one[- ]piece|domestic|exceptions?|reduced)\b", re.I)


def route_list_unasked(query_text: str, chunk_text: str) -> bool:
    """True when the chunk is a list of specific routes but the passenger named
    no place and did not ask about routes. Such a list is an exception to the
    general rule ("on Nairobi to Mumbai ... one piece"), and a passenger asking
    "what is the baggage allowance?" wants the general rule first."""
    if mentioned(query_text) or _ROUTE_WORDS_RE.search(query_text or ""):
        return False
    return sum(1 for p in mentioned(chunk_text) if is_city(p)) >= 2
