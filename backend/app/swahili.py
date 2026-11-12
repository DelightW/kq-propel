"""Swahili questions, read through a small travel glossary.

The policy documents and the composer are English only, so this does not
answer in Swahili. It recognises a Swahili question, turns the travel terms it
knows into an English search ("Ninaweza kubeba mizigo mingapi kwenye Economy
kwenda London?" becomes "how many baggage check Economy to London"), and the
answer is given in English from the English policy, with a line in Swahili
saying so and showing how the question was understood. A question the glossary
cannot make enough of is declined in Swahili rather than guessed at.

The glossary is deliberately small and covers the topics the corpus covers;
it is not a translator.
"""
import re
from typing import Optional

# Common Swahili words. A message is read as Swahili when several of them
# appear, so a stray "na" or "ya" in English does not trigger it.
_MARKERS = frozenset("""
ni na kwa ya za wa la kwenye katika gani ngapi mingapi ninaweza naweza je nini wapi lini
mimi yangu wangu langu changu tafadhali habari sana hii hiyo kama pia au bila kuna ina
kwenda kutoka safari mizigo mzigo ndege saa ada tiketi hufungwa nataka nahitaji ninahitaji
kubeba kuingia kimataifa ndani imechelewa imepotea kulipa gharama jina kubadilisha
kughairi uzito kiasi bei pesa marejesho uwanja daraja kiti mtoto
yako yake leo hali hewa nifanye nini sasa bado ninaweza
""".split())

# Longest phrases first, so "saa ngapi" is read before "ngapi".
_GLOSSARY = [
    ("kaunta za kuingia", "check-in counters"),
    ("mizigo ya mkononi", "hand baggage"),
    ("mzigo wa mkononi", "hand baggage"),
    ("safari za kimataifa", "international flights"),
    ("safari ya kimataifa", "international flight"),
    ("safari za ndani", "domestic flights"),
    ("safari ya ndani", "domestic flight"),
    ("uwanja wa ndege", "airport"),
    ("kiti cha magurudumu", "wheelchair"),
    ("mtoto mchanga", "infant"),
    ("daraja la biashara", "Business class"),
    ("daraja la kawaida", "Economy"),
    ("kurejeshewa pesa", "refund"),
    ("kurudishiwa pesa", "refund"),
    ("saa ngapi", "what time"),
    ("kiasi gani", "how much"),
    ("bei gani", "how much"),
    ("pesa ngapi", "how much"),
    ("kabla ya", "before"),
    ("mizigo", "baggage"),
    ("mzigo", "bag"),
    ("mingapi", "how many"),
    ("ngapi", "how many"),
    ("kubeba", "check"),
    ("kuingia", "check-in"),
    ("hufungwa", "close"),
    ("inafungwa", "close"),
    ("zinafungwa", "close"),
    ("kwenda", "to"),
    ("kutoka", "from"),
    ("ndege", "flight"),
    ("imechelewa", "delayed"),
    ("kuchelewa", "delay"),
    ("imepotea", "lost"),
    ("umepotea", "lost"),
    ("haujafika", "not arrived"),
    ("haijafika", "not arrived"),
    ("marejesho", "refund"),
    ("tiketi", "ticket"),
    ("kubadilisha", "change"),
    ("kughairi", "cancel"),
    ("jina", "name"),
    ("ada", "fee"),
    ("gharama", "cost"),
    ("malipo", "payment"),
    ("kulipa", "pay"),
    ("uzito", "weight"),
    ("ziada", "excess"),
    ("fidia", "compensation"),
    ("pasipoti", "passport"),
    ("mapema", "early"),
    ("mtoto", "child"),
]

# Shown above the English answer: "For now I answer in English only. I
# understood your question as: ..."
ANSWER_NOTE = "Kwa sasa ninajibu kwa Kiingereza pekee. Nimeelewa swali lako kama: \"{english}\"."
# "Sorry, I don't understand Swahili well yet. Please ask in English, for
# example about baggage, check-in, delays or refunds."
DECLINE = ("Samahani, bado sielewi Kiswahili vizuri. Tafadhali uliza kwa Kiingereza, kwa mfano "
           "kuhusu mizigo, kuingia (check-in), kuchelewa kwa ndege au kurejeshewa pesa.\n\n"
           "Sorry, I can't read Swahili well enough yet to answer that. Please ask in English, "
           "for example about baggage, check-in, delays or refunds.")

_WORD_RE = re.compile(r"[A-Za-z']+")


def is_swahili(message: str) -> bool:
    words = [w.lower() for w in _WORD_RE.findall(message or "")]
    if len(words) < 3:
        return False
    hits = {w for w in words if w in _MARKERS}
    return len(hits) >= 3 and len([w for w in words if w in _MARKERS]) / len(words) >= 0.3


def to_english(message: str) -> Optional[str]:
    """The English search the glossary makes of a Swahili question, or None
    when it recognises fewer than two travel terms."""
    text = " " + " ".join(_WORD_RE.findall(message or "")) + " "
    lowered = text.lower()
    found = []                                    # (position, English)
    for phrase, english in _GLOSSARY:
        for m in re.finditer(r"(?<= )" + re.escape(phrase) + r"(?= )", lowered):
            found.append((m.start(), english))
            lowered = lowered[:m.start()] + "#" * len(phrase) + lowered[m.end():]
    # Words the glossary does not know but that are clearly not Swahili - a
    # place or a cabin name written with a capital ("London", "Economy").
    for m in re.finditer(r"[A-Za-z']+", text):
        word = m.group(0)
        if lowered[m.start():m.end()] != word.lower():
            continue                              # already translated
        if word[0].isupper() and word.lower() not in _MARKERS and m.start() > 1:
            found.append((m.start(), word))
    terms = [e for _, e in sorted(found)]
    travel_terms = [t for t in terms if t not in ("to", "from", "before")]
    if len(travel_terms) < 2:
        return None
    return " ".join(terms)
