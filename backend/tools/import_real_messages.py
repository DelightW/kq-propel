"""
Adds consented, anonymised passenger messages to the frustration dataset.

The classifier was trained on messages written for this study. Retraining on
real messages from Kenya Airways' channels (including Swahili and Sheng) is the
way to find out whether it works on real complaints. This tool is the only
door those messages should come through, and it enforces what must be true of
them before they touch the training file:

  * every row has a consent reference (the record that the passenger agreed to
    this use); rows without one are rejected, not imported;
  * the label is "calm" or "frustrated";
  * phone numbers, email addresses, booking references, ticket and card
    numbers are replaced with placeholders. This is a second line of defence:
    the export must already be anonymised, because names and addresses cannot
    be detected reliably by a pattern;
  * messages already in the training file or the challenge set are dropped,
    so a test sentence can never leak into training.

Input CSV columns: text, label, consent_ref, and optionally language
(en, sw, sheng...). The dataset gains `language` and `source` columns; existing
rows are marked language=en, source=authored. The classifier retrains itself on
the next start because the dataset fingerprint changes.

Run:  python tools/import_real_messages.py export.csv            (dry run)
      python tools/import_real_messages.py export.csv --write    (import)
Then: python tools/classifier_report.py
"""
import argparse
import csv
import re
import sys
from collections import Counter
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app import config, privacy  # noqa: E402

DATASET = config.SENTIMENT_DATASET_PATH
CHALLENGE = DATASET.with_name("frustration_challenge.csv")
LABELS = {"calm", "frustrated"}
FIELDS = ["text", "label", "language", "source"]

_SCRUB = [
    (privacy.PHONE_RE, "[phone]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
    (re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)"), "[number]"),         # ticket and card numbers
    (re.compile(r"\b(?=[A-Z0-9]{6}\b)(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{6}\b"), "[booking ref]"),
]


def scrub(text: str):
    found = []
    for pattern, placeholder in _SCRUB:
        text, n = pattern.subn(placeholder, text)
        if n:
            found.append(placeholder)
    return text, found


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _read(path: Path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("export", type=Path, help="CSV of consented, anonymised messages")
    ap.add_argument("--write", action="store_true", help="import (default is a dry run)")
    args = ap.parse_args()

    existing = _read(DATASET)
    seen = {_norm(r["text"]) for r in existing}
    seen |= {_norm(r["text"]) for r in _read(CHALLENGE)} if CHALLENGE.exists() else set()

    accepted, rejected, scrubbed = [], Counter(), Counter()
    for i, row in enumerate(_read(args.export), start=2):
        text = (row.get("text") or "").strip()
        label = (row.get("label") or "").strip().lower()
        if not (row.get("consent_ref") or "").strip():
            rejected["no consent reference"] += 1
            continue
        if not text:
            rejected["empty text"] += 1
            continue
        if label not in LABELS:
            rejected[f"label not calm/frustrated (line {i})" if len(rejected) < 5 else "bad label"] += 1
            continue
        text, found = scrub(text)
        for f in found:
            scrubbed[f] += 1
        if _norm(text) in seen:
            rejected["duplicate of training or challenge data"] += 1
            continue
        seen.add(_norm(text))
        accepted.append({"text": text, "label": label,
                         "language": (row.get("language") or "unknown").strip().lower() or "unknown",
                         "source": "real"})

    by = Counter((r["language"], r["label"]) for r in accepted)
    print(f"Accepted {len(accepted)} message(s):")
    for (lang, label), n in sorted(by.items()):
        print(f"  {lang:10} {label:11} {n}")
    if rejected:
        print("Rejected:")
        for reason, n in rejected.items():
            print(f"  {n:5}  {reason}")
    if scrubbed:
        print("Replaced with placeholders: " + ", ".join(f"{k} x{n}" for k, n in scrubbed.items()))

    if not args.write:
        print("\nDry run: nothing written. Add --write to import.")
        return 0
    if not accepted:
        print("Nothing to import.")
        return 1
    rows = [{"text": r["text"], "label": r["label"], "language": r.get("language") or "en",
             "source": r.get("source") or "authored"} for r in existing] + accepted
    with open(DATASET, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"\nWrote {len(rows)} rows to {DATASET}. The classifier retrains on the next start;"
          " run tools/classifier_report.py to see its errors by class and language.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
