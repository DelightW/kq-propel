"""
Diagnoses what the frustration classifier actually learned.

A held-out split of a synthetic dataset can only tell you that the model
learned the dataset. It cannot tell you whether the dataset taught the right
thing. This script probes for the specific failure that kind of evaluation is
blind to: a spurious surface feature that separates the classes in the data
but has nothing to do with frustration.

Run: python tools/diagnose_sentiment.py
"""
import csv
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app import config, sentiment  # noqa: E402


def score(text: str) -> float:
    return sentiment.classify_frustration(text)["frustration_score"]


def dataset_rows():
    with open(config.SENTIMENT_DATASET_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def report_label_artifacts(rows) -> None:
    calm = [r["text"] for r in rows if r["label"] == "calm"]
    frustrated = [r["text"] for r in rows if r["label"] == "frustrated"]
    print(f"\nDataset: {len(calm)} calm / {len(frustrated)} frustrated")
    print("\nSurface features, by class:")
    print(f"  {'feature':<22}{'calm':>10}{'frustrated':>14}")
    checks = {
        "ends with '?'": lambda t: t.strip().endswith("?"),
        "contains '?'": lambda t: "?" in t,
        "contains '!'": lambda t: "!" in t,
        "contains a comma": lambda t: "," in t,
        "starts 'I '": lambda t: t.startswith("I "),
        "has ALLCAPS word": lambda t: any(w.isupper() and len(w) > 2 for w in t.split()),
    }
    for name, fn in checks.items():
        c = sum(fn(t) for t in calm) / max(len(calm), 1)
        f = sum(fn(t) for t in frustrated) / max(len(frustrated), 1)
        flag = "  <-- perfectly separating" if (c == 0 and f > 0) or (f == 0 and c > 0) else ""
        print(f"  {name:<22}{c:>9.0%}{f:>13.0%}{flag}")


def probe_question_mark() -> list:
    """The decisive test: change only the punctuation and nothing else.

    If frustration is what the model measures, a question mark cannot move the
    score much. If the score jumps, the model is reading punctuation.
    """
    failures = []
    print("\nSame sentence, one character different:")
    print(f"  {'sentence':<62}{'no ?':>8}{'with ?':>9}{'delta':>8}")
    neutral_sentences = [
        "How much does it cost to book a ticket by telephone",
        "What is the baggage allowance in economy class",
        "Can you tell me what time check-in opens",
        "I would like to know the refund policy",
        "How do I pay for extra baggage",
    ]
    deltas = []
    for base in neutral_sentences:
        without = score(base)
        with_q = score(base + "?")
        deltas.append(with_q - without)
        print(f"  {base[:60]:<62}{without:>8.3f}{with_q:>9.3f}{with_q - without:>+8.3f}")
    avg = sum(deltas) / len(deltas)
    print(f"\n  Mean effect of adding '?': {avg:+.3f}")
    # Checked in both directions. An earlier version tested only avg > 0.15 and
    # so reported a clean bill of health for a model in which '?' pushed
    # strongly towards calm - the same artifact with its sign reversed.
    if abs(avg) > 0.15:
        direction = "frustrated" if avg > 0 else "calm"
        print(f"  VERDICT: the model is reading punctuation, not frustration")
        print(f"           ('?' alone moves it towards {direction}).")
        failures.append(f"punctuation effect {avg:+.3f} exceeds +/-0.15")
    else:
        print("  VERDICT: punctuation alone does not drive the prediction.")
    return failures


def probe_unambiguous_cases() -> None:
    """Sentences whose label no reasonable person would dispute."""
    print("\nUnambiguous cases:")
    cases = [
        ("frustrated", "I am furious, nobody has helped me at all"),
        ("frustrated", "Worst airline ever, I want my money back now"),
        ("frustrated", "This is a disgrace, three days and still no bag"),
        ("frustrated", "I have been ignored for hours and I am done with this"),
        ("calm", "How much does it cost to book a ticket by telephone?"),
        ("calm", "Good morning, what is the check-in time for Nairobi?"),
        ("calm", "Could you confirm the cabin baggage size please?"),
        ("calm", "Thanks, and how much is the name change fee?"),
    ]
    wrong = 0
    for expected, text in cases:
        s = score(text)
        got = "frustrated" if s >= 0.5 else "calm"
        ok = got == expected
        wrong += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'WRONG'}  expected={expected:<11}got={got:<11}"
              f"score={s:.3f}  {text[:48]}")
    print(f"\n  {wrong} of {len(cases)} wrong on cases with no reasonable ambiguity.")
    return wrong


def main() -> int:
    print("=" * 78)
    print("Frustration classifier diagnosis")
    print("=" * 78)
    metrics = sentiment.get_metrics()
    print(f"\nReported on a held-out split of the training data:")
    print(f"  accuracy={metrics['accuracy']}  precision={metrics['precision']}  "
          f"recall={metrics['recall']}  f1={metrics['f1_score']}")
    challenge = metrics.get("challenge")
    if challenge:
        print(f"\nOn the challenge set (never trained on, different wording):")
        print(f"  accuracy={challenge['accuracy']}  precision={challenge['precision']}  "
              f"recall={challenge['recall']}  f1={challenge['f1_score']}")
        print(f"  generalization gap = {metrics.get('generalization_gap')}")
    else:
        print("\n  NOTE: no challenge set found - only in-distribution numbers.")

    report_label_artifacts(dataset_rows())
    failures = probe_question_mark()
    wrong = probe_unambiguous_cases()

    print("\n" + "=" * 78)
    if wrong or failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        if wrong:
            print(f"  - {wrong} unambiguous case(s) misclassified")
    else:
        print("PASS: no punctuation artifact, and every unambiguous case correct.")
    print("\nNote that a held-out split of the same synthetic file cannot detect")
    print("an artifact, because the artifact is present in the test half too.")
    print("That is what the challenge set above is for.")
    print("=" * 78)
    return 1 if (wrong or failures) else 0


if __name__ == "__main__":
    raise SystemExit(main())
