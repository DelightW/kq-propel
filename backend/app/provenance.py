"""
Policy document provenance: where each document came from and which edition
of it the knowledge base holds.

Read from data/policies/provenance.json. Each entry carries:

  provenance      kenya_airways_published | synthetic
  source_url      the page the content was taken from
  retrieved_on    when it was taken (YYYY-MM-DD)
  version         the edition held, bumped whenever the text changes
  effective_from  the date Kenya Airways says the policy took effect (YYYY-MM-DD);
                  empty until the airline confirms it

The edition is shown in every citation so a passenger, or a member of staff
handling a dispute, can tell exactly which version of a policy an answer
relied on.
"""
import json
from datetime import date
from functools import lru_cache
from typing import Dict, Optional

from app import config


@lru_cache(maxsize=1)
def _manifest() -> Dict[str, Dict]:
    path = config.POLICY_DIR / "provenance.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {d["file"]: d for d in data.get("documents", []) if d.get("file")}


def document(source: str) -> Optional[Dict]:
    """Manifest entry for a document, by file name ("refund_policy.txt"),
    document id ("refund_policy") or display title ("Refund Policy")."""
    if not source:
        return None
    name = source.strip()
    if not name.endswith(".txt"):
        name = name.lower().replace(" ", "_") + ".txt"
    return _manifest().get(name)


def _day(iso: Optional[str]) -> Optional[str]:
    try:
        return date.fromisoformat(iso).strftime("%d %b %Y").lstrip("0") if iso else None
    except ValueError:
        return None


def edition_label(source: str) -> str:
    """'version 1.0, effective 1 Oct 2026' or 'version 1.0, as published 23 Sep 2026'.

    Empty when the document has no manifest entry. Synthetic documents are
    labelled as prototype material rather than given a publication date.
    """
    entry = document(source)
    if not entry:
        return ""
    parts = [f"version {entry['version']}"] if entry.get("version") else []
    if entry.get("provenance") == "synthetic":
        parts.append("prototype document, not airline policy")
    elif _day(entry.get("effective_from")):
        parts.append(f"effective {_day(entry['effective_from'])}")
    elif _day(entry.get("retrieved_on")):
        parts.append(f"as published {_day(entry['retrieved_on'])}")
    return ", ".join(parts)
