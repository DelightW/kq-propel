"""
Errors of the frustration classifier, by class and by language.

The dashboard shows summary figures. This report shows what the summary hides:
how each class is read, which messages were misclassified and with what score,
and - once real messages with a `language` column have been imported - how the
model does on Swahili or Sheng compared with English. A model that is accurate
overall can still fail one language or one kind of passenger.

Run:  python tools/classifier_report.py [--show N]
"""
import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sklearn.model_selection import train_test_split  # noqa: E402

from app import config, sentiment  # noqa: E402


def _rows(path: Path):
    with open(path, newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r.get("text")]


def report(name, rows, model, show):
    texts = [r["text"] for r in rows]
    labels = [r["label"] for r in rows]
    scores = model.predict_proba(texts)[:, list(model.classes_).index("frustrated")]
    pred = ["frustrated" if s >= 0.5 else "calm" for s in scores]
    pc = sentiment._per_class(labels, pred)
    print(f"\n=== {name} (n = {len(rows)}) ===")
    print(f"  {'class':11} {'precision':>9} {'recall':>7} {'f1':>6} {'n':>5}")
    for c in sentiment.CLASSES:
        m = pc[c]
        print(f"  {c:11} {m['precision']:9.3f} {m['recall']:7.3f} {m['f1_score']:6.3f} {m['support']:5}")
    e = pc["errors"]
    print(f"  errors: {e['calm_read_as_frustrated']} calm read as frustrated, "
          f"{e['frustrated_read_as_calm']} frustrated read as calm")

    by_lang = defaultdict(list)
    for r, p in zip(rows, pred):
        by_lang[(r.get("language") or "en")].append(r["label"] == p)
    if len(by_lang) > 1:
        print("  accuracy by language: " + ", ".join(
            f"{k} {sum(v) / len(v):.3f} (n={len(v)})" for k, v in sorted(by_lang.items())))

    wrong = sorted(((s, r) for s, r, p in zip(scores, rows, pred) if r["label"] != p),
                   key=lambda t: -abs(t[0] - 0.5))
    if wrong and show:
        print(f"  most confident mistakes (score = probability of frustrated):")
        for s, r in wrong[:show]:
            print(f"    {s:.2f}  [{r['label']}]  {r['text'][:100]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", type=int, default=8, help="misclassified messages to list per set")
    args = ap.parse_args()

    sentiment.train_frustration_classifier()
    model = sentiment._get_model()
    rows = _rows(config.SENTIMENT_DATASET_PATH)
    # The same split as training (seed 42, stratified), so this is the held-out part.
    _, test = train_test_split(rows, test_size=0.25, random_state=42,
                               stratify=[r["label"] for r in rows])
    report("Held-out split", test, model, args.show)
    challenge = config.SENTIMENT_DATASET_PATH.with_name("frustration_challenge.csv")
    if challenge.exists():
        report("Challenge set (never trained on)", _rows(challenge), model, args.show)


if __name__ == "__main__":
    main()
