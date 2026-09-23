"""
KQ-Propel synthetic data generation and provenance pipeline.

This script is the reproducible record of how every dataset in the project was
constructed. It is deliberately deterministic: given the same --seed it emits
byte-identical output, so any result reported in the dissertation can be
regenerated and audited.

The project uses a synthetic corpus by design. Real airline policy documents
are copyrighted and their fee schedules change without notice, which makes them
unsuitable as a reproducible academic ground truth. Authoring the corpus
instead means the correct answer to every evaluation question is known exactly,
which is what makes groundedness measurable rather than subjective.

Three stages:

  Stage A - FEE REGISTRY
      A single declarative schedule of every monetary and temporal value in the
      domain. The policy corpus and the evaluation set are both validated
      against it, so a figure can never silently drift between the document a
      passenger is quoted and the key the evaluator expects.

  Stage B - FRUSTRATION DATASET
      A construction grammar (markers x templates x slot fillers) expanded
      combinatorially under a fixed RNG seed, then deduplicated and balanced.
      The shipped dataset is the human-reviewed release of this pool.

  Stage C - TRAIN AND EVALUATE
      Replicates backend/app/sentiment.py exactly (TF-IDF + stylistic feature
      union -> Logistic Regression, stratified 75/25, random_state=42) so the
      metrics quoted here are the metrics the running system reports.

Usage:
    python generate_datasets.py --report          # audit shipped data
    python generate_datasets.py --regenerate      # build a fresh pool
    python generate_datasets.py --regenerate --overwrite   # replace shipped CSV
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import random
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


def _find_backend() -> Path:
    """Locate the backend directory regardless of where this script lives.

    Works when docgen/ sits inside the repository (Colab clone) and when it
    sits alongside it (local working copy).
    """
    here = Path(__file__).resolve()
    candidates = []
    for parent in [here.parent, *here.parents]:
        candidates.append(parent / "backend")
        candidates.append(parent / "kq-propel" / "backend")
    for candidate in candidates:
        if (candidate / "data" / "policies").is_dir():
            return candidate
    raise FileNotFoundError(
        "Could not locate the KQ-Propel backend directory. Run this script from "
        "inside the repository, or set BACKEND manually."
    )


BACKEND = _find_backend()
DATA = BACKEND / "data"
POLICY_DIR = DATA / "policies"
SENTIMENT_CSV = DATA / "sentiment_dataset" / "frustration_dataset.csv"
EVAL_JSON = DATA / "eval" / "evaluation_dataset.json"
OUT_DIR = Path(__file__).resolve().parent / "generated"

TARGET_PER_CLASS = 50
SEED = 42


# ---------------------------------------------------------------------------
# Stage A: the ground-truth fee registry
# ---------------------------------------------------------------------------

PROVENANCE_JSON = POLICY_DIR / "provenance.json"

KQ_BAGGAGE_URL = "https://www.kenya-airways.com/en/fly/plan/baggage/"
KQ_REFUNDS_URL = "https://www.kenya-airways.com/en/fly/manage/refunds/"
KQ_CHECKIN_URL = "https://www.kenya-airways.com/en/fly/prepare/check-in/"
RETRIEVED_ON = "2026-09-23"

# Every figure below is published by Kenya Airways. Where the source table
# carries no currency label, `unit` is recorded as "unlabelled" rather than
# guessed - see the currency caveat in provenance.json.
FEE_REGISTRY: Dict[str, Dict] = {
    "checked_allowance_economy_pieces": {
        "value": "two pieces", "unit": "count", "document": "baggage_allowance_policy",
        "condition": "Economy, Africa to Europe/Americas/Middle East/Asia and within Africa",
        "provenance": "kenya_airways_published", "source_url": KQ_BAGGAGE_URL,
        "retrieved_on": RETRIEVED_ON,
    },
    "cabin_allowance_economy_weight": {
        "value": "12kg", "unit": "kg", "document": "hand_baggage_policy",
        "condition": "Economy cabin piece to or from the EU, US and UK",
        "provenance": "kenya_airways_published", "source_url": KQ_BAGGAGE_URL,
        "retrieved_on": RETRIEVED_ON,
    },
    "heavy_bag_europe_from_hub": {
        "value": "75", "unit": "unlabelled", "document": "excess_baggage_fees_policy",
        "condition": "heavy bag fee, Europe, journey beginning at the Nairobi hub",
        "provenance": "kenya_airways_published", "source_url": KQ_BAGGAGE_URL,
        "retrieved_on": RETRIEVED_ON,
    },
    "heavy_bag_europe_other_points": {
        "value": "100", "unit": "unlabelled", "document": "excess_baggage_fees_policy",
        "condition": "heavy bag fee, Europe, journey beginning at all other points",
        "provenance": "kenya_airways_published", "source_url": KQ_BAGGAGE_URL,
        "retrieved_on": RETRIEVED_ON,
    },
    "sporting_equipment_africa_europe": {
        "value": "110", "unit": "unlabelled", "document": "special_baggage_policy",
        "condition": "golf, bicycle, diving, surf, ski equipment between Africa and Europe or the Americas",
        "provenance": "kenya_airways_published", "source_url": KQ_BAGGAGE_URL,
        "retrieved_on": RETRIEVED_ON,
    },
    "first_needs_allowance_premium": {
        "value": "150 US dollars", "unit": "USD", "document": "delayed_baggage_policy",
        "condition": "First Class and Premier World, baggage not received within 24 hours",
        "provenance": "kenya_airways_published", "source_url": KQ_BAGGAGE_URL,
        "retrieved_on": RETRIEVED_ON,
    },
    "first_needs_allowance_economy": {
        "value": "100 US dollars", "unit": "USD", "document": "delayed_baggage_policy",
        "condition": "Economy, baggage not received within 24 hours",
        "provenance": "kenya_airways_published", "source_url": KQ_BAGGAGE_URL,
        "retrieved_on": RETRIEVED_ON,
    },
    "refund_processing_window": {
        "value": "21 to 30 business days", "unit": "days", "document": "refund_policy",
        "condition": "refund to the original credit or debit card",
        "provenance": "kenya_airways_published", "source_url": KQ_REFUNDS_URL,
        "retrieved_on": RETRIEVED_ON,
    },
    "name_correction_international": {
        "value": "75\nUS dollars", "unit": "USD", "document": "refund_policy",
        "condition": "correction of up to three characters, international booking",
        "provenance": "kenya_airways_published", "source_url": KQ_REFUNDS_URL,
        "retrieved_on": RETRIEVED_ON,
    },
    "domestic_counter_closing": {
        "value": "30 minutes", "unit": "minutes", "document": "checkin_policy",
        "condition": "domestic check-in counter closing time before departure",
        "provenance": "kenya_airways_published", "source_url": KQ_CHECKIN_URL,
        "retrieved_on": RETRIEVED_ON,
    },
}


def audit_fee_registry() -> List[str]:
    """Every declared figure must actually appear in its source document."""
    problems: List[str] = []
    for key, entry in FEE_REGISTRY.items():
        doc = POLICY_DIR / f"{entry['document']}.txt"
        if not doc.exists():
            problems.append(f"{key}: source document {doc.name} is missing")
            continue
        # Source documents are hard-wrapped, so a multi-word figure can straddle
        # a line break. Collapse whitespace on both sides before matching.
        haystack = re.sub(r"\s+", " ", doc.read_text(encoding="utf-8"))
        needle = re.sub(r"\s+", " ", entry["value"])
        if needle not in haystack:
            problems.append(
                f"{key}: {entry['unit']} {needle!r} not found in {doc.name}"
            )
        if entry.get("provenance") == "kenya_airways_published" and not entry.get("source_url"):
            problems.append(f"{key}: published figure declares no source_url")
    return problems


def audit_provenance_manifest() -> Tuple[List[str], Dict]:
    """Every indexed document must be accounted for in provenance.json."""
    problems: List[str] = []
    if not PROVENANCE_JSON.exists():
        return [f"{PROVENANCE_JSON.name} is missing"], {}

    manifest = json.loads(PROVENANCE_JSON.read_text(encoding="utf-8"))
    declared = {d["file"]: d for d in manifest.get("documents", [])}
    on_disk = {p.name for p in POLICY_DIR.glob("*.txt")}

    for name in sorted(on_disk - set(declared)):
        problems.append(f"{name} is indexed but has no provenance entry")
    for name in sorted(set(declared) - on_disk):
        problems.append(f"{name} is declared in the manifest but is not on disk")

    counts: Dict[str, int] = {}
    for name, entry in declared.items():
        if name not in on_disk:
            continue
        kind = entry.get("provenance", "undeclared")
        counts[kind] = counts.get(kind, 0) + 1
        if kind == "kenya_airways_published":
            if not entry.get("source_url"):
                problems.append(f"{name}: published document declares no source_url")
            if not entry.get("retrieved_on"):
                problems.append(f"{name}: published document declares no retrieved_on")
        elif kind == "synthetic":
            body = (POLICY_DIR / name).read_text(encoding="utf-8").lower()
            if "synthetic" not in body:
                problems.append(
                    f"{name}: synthetic document does not disclose its status in its body"
                )

    stats = {"documents": len(on_disk), "by_provenance": counts}
    return problems, stats


def audit_evaluation_set() -> Tuple[List[str], Dict]:
    """Each expected_keyword must be answerable from the declared topics."""
    problems: List[str] = []
    items = json.loads(EVAL_JSON.read_text(encoding="utf-8"))
    multi_hop = 0

    for item in items:
        topics = item.get("expected_topics", [])
        if len(topics) > 1:
            multi_hop += 1
        corpus = ""
        for topic in topics:
            doc = POLICY_DIR / f"{topic}.txt"
            if not doc.exists():
                problems.append(f"{item['id']}: unknown topic '{topic}'")
                continue
            corpus += doc.read_text(encoding="utf-8")
        # Source documents are hard-wrapped, so a multi-word expected answer can
        # straddle a line break. Collapse whitespace before matching.
        corpus = re.sub(r"\s+", " ", corpus)
        for keyword in item.get("expected_keywords", []):
            if re.search(re.escape(re.sub(r"\s+", " ", keyword)), corpus, re.IGNORECASE):
                continue
            problems.append(
                f"{item['id']}: keyword '{keyword}' is not present in {topics}"
            )

    stats = {
        "questions": len(items),
        "multi_hop_questions": multi_hop,
        "single_hop_questions": len(items) - multi_hop,
        "total_expected_keywords": sum(len(i.get("expected_keywords", [])) for i in items),
    }
    return problems, stats


# ---------------------------------------------------------------------------
# Stage B: the frustration construction grammar
# ---------------------------------------------------------------------------
#
# Each class is defined by the pragmatic markers that distinguish it, not by
# free-form invention. Frustrated utterances are built from four documented
# markers found in the customer-service literature: repetition of a failure,
# lexical intensification, time-cost complaints, and escalation demands.
# Calm utterances are matched for topic and length so the classifier cannot
# separate the classes on subject matter alone.

GRIEVANCES = [
    "my flight has been delayed again",
    "my baggage has been lost",
    "my connecting flight was missed",
    "my refund has still not arrived",
    "my booking was cancelled without warning",
    "my seat was given away at the gate",
    "my bag was damaged in transit",
    "my payment was taken twice",
]

INTENSIFIERS = [
    "this is completely unacceptable",
    "I am absolutely furious",
    "this is ridiculous",
    "I am extremely upset",
    "this is a total disgrace",
    "I have had enough of this",
]

TIME_COSTS = [
    "I have been on hold for two hours",
    "I have been waiting at the counter since morning",
    "I have been chasing this for three weeks",
    "nobody has called me back in four days",
]

DEMANDS = [
    "I demand compensation right now",
    "I want to speak to a supervisor immediately",
    "I expect this fixed today",
    "escalate this to your manager",
]

NEGLECT = [
    "nobody is telling me what is happening",
    "no one is helping me",
    "nobody will give me a straight answer",
    "your staff keep passing me around",
]

ORDINALS = ["second", "third", "fourth"]

FRUSTRATED_TEMPLATES = [
    "{grievance} and {neglect}",
    "This is the {ordinal} time {grievance}, {intensifier}",
    "{time_cost} and {neglect}, {intensifier}",
    "{grievance}. {demand}",
    "{intensifier}, {grievance} and {neglect}",
    "{time_cost}. {demand}",
    "Why is this so complicated, {grievance} and {neglect}",
    "{grievance}, {intensifier} and I want it resolved",
]

TOPICS = [
    "the baggage allowance for economy class",
    "the overweight baggage fee",
    "how to pay for excess baggage",
    "when online check-in closes",
    "the refund processing time",
    "how to change my travel date",
    "what compensation applies to a delay",
    "the boarding gate closing time",
]

POLITE_OPENERS = [
    "Hello, could you please tell me",
    "Good morning, I would like to know",
    "Hi, I just wanted to check",
    "Please could you confirm",
    "I hope you can help me understand",
    "May I ask about",
]

CALM_CLOSERS = [
    "Thank you for your help.",
    "Thanks in advance.",
    "Much appreciated.",
    "No rush at all.",
]

CALM_TEMPLATES = [
    "{opener} {topic}?",
    "{opener} {topic}? {closer}",
    "I am planning my trip and would like to understand {topic}.",
    "Quick question about {topic} if you have a moment.",
    "{opener} {topic}. {closer}",
    "Could someone clarify {topic} for me please?",
]


def _expand(templates: Iterable[str], pools: Dict[str, List[str]],
            rng: random.Random, target: int) -> List[str]:
    """Deterministically expand a template set against its slot pools."""
    candidates: List[str] = []
    for template in templates:
        slots = re.findall(r"\{(\w+)\}", template)
        if not slots:
            candidates.append(template)
            continue
        for combo in itertools.product(*(pools[s] for s in slots)):
            candidates.append(template.format(**dict(zip(slots, combo))))

    seen, unique = set(), []
    for text in candidates:
        text = re.sub(r"\s+", " ", text).strip()
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(text)

    rng.shuffle(unique)
    return unique[:target]


def generate_frustration_dataset(seed: int = SEED,
                                 per_class: int = TARGET_PER_CLASS) -> List[Dict[str, str]]:
    rng = random.Random(seed)

    frustrated = _expand(FRUSTRATED_TEMPLATES, {
        "grievance": GRIEVANCES,
        "intensifier": INTENSIFIERS,
        "time_cost": TIME_COSTS,
        "demand": DEMANDS,
        "neglect": NEGLECT,
        "ordinal": ORDINALS,
    }, rng, per_class)

    calm = _expand(CALM_TEMPLATES, {
        "opener": POLITE_OPENERS,
        "topic": TOPICS,
        "closer": CALM_CLOSERS,
    }, rng, per_class)

    rows = ([{"text": t, "label": "frustrated"} for t in frustrated]
            + [{"text": t, "label": "calm"} for t in calm])
    rng.shuffle(rows)
    return rows


def theoretical_pool_size() -> Dict[str, int]:
    """How many distinct utterances the grammar can produce per class."""
    pools = {
        "grievance": GRIEVANCES, "intensifier": INTENSIFIERS, "time_cost": TIME_COSTS,
        "demand": DEMANDS, "neglect": NEGLECT, "ordinal": ORDINALS,
    }
    frustrated = sum(
        _product([len(pools[s]) for s in re.findall(r"\{(\w+)\}", t)])
        for t in FRUSTRATED_TEMPLATES
    )
    calm_pools = {"opener": POLITE_OPENERS, "topic": TOPICS, "closer": CALM_CLOSERS}
    calm = sum(
        _product([len(calm_pools[s]) for s in re.findall(r"\{(\w+)\}", t)])
        for t in CALM_TEMPLATES
    )
    return {"frustrated": frustrated, "calm": calm}


def _product(values: List[int]) -> int:
    total = 1
    for v in values:
        total *= v
    return total


# ---------------------------------------------------------------------------
# Stage C: train and evaluate, mirroring backend/app/sentiment.py
# ---------------------------------------------------------------------------

def train_and_score(rows: List[Dict[str, str]]) -> Dict:
    sys.path.insert(0, str(BACKEND))
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import (accuracy_score, f1_score, precision_score,
                                 recall_score)
    from sklearn.model_selection import train_test_split
    from sklearn.pipeline import FeatureUnion, Pipeline
    from sklearn.preprocessing import StandardScaler

    from app.sentiment import StyleFeatures

    texts = [r["text"] for r in rows]
    labels = [r["label"] for r in rows]

    x_train, x_test, y_train, y_test = train_test_split(
        texts, labels, test_size=0.25, random_state=42, stratify=labels
    )

    pipeline = Pipeline([
        ("features", FeatureUnion([
            ("tfidf", TfidfVectorizer(ngram_range=(1, 2), min_df=1, sublinear_tf=True)),
            ("style", Pipeline([("raw", StyleFeatures()), ("scale", StandardScaler())])),
        ])),
        ("clf", LogisticRegression(max_iter=2000, class_weight="balanced")),
    ])
    pipeline.fit(x_train, y_train)
    y_pred = pipeline.predict(x_test)

    return {
        "accuracy": round(float(accuracy_score(y_test, y_pred)), 4),
        "precision": round(float(precision_score(y_test, y_pred,
                                                 pos_label="frustrated", zero_division=0)), 4),
        "recall": round(float(recall_score(y_test, y_pred,
                                           pos_label="frustrated", zero_division=0)), 4),
        "f1_score": round(float(f1_score(y_test, y_pred,
                                         pos_label="frustrated", zero_division=0)), 4),
        "train_size": len(x_train),
        "test_size": len(x_test),
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def load_shipped() -> List[Dict[str, str]]:
    with open(SENTIMENT_CSV, newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r.get("text")]


def describe(rows: List[Dict[str, str]], name: str) -> Dict:
    lengths = [len(r["text"].split()) for r in rows]
    counts: Dict[str, int] = {}
    for r in rows:
        counts[r["label"]] = counts.get(r["label"], 0) + 1
    return {
        "dataset": name,
        "rows": len(rows),
        "class_balance": counts,
        "mean_words": round(sum(lengths) / len(lengths), 1) if lengths else 0,
        "min_words": min(lengths) if lengths else 0,
        "max_words": max(lengths) if lengths else 0,
        "duplicates": len(rows) - len({r["text"].lower() for r in rows}),
    }


def write_csv(rows: List[Dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["text", "label"], quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--report", action="store_true",
                        help="audit the shipped corpus, evaluation set and dataset")
    parser.add_argument("--regenerate", action="store_true",
                        help="rebuild the frustration dataset from the grammar")
    parser.add_argument("--overwrite", action="store_true",
                        help="with --regenerate, replace the shipped dataset in place")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--per-class", type=int, default=TARGET_PER_CLASS)
    args = parser.parse_args()

    if not (args.report or args.regenerate):
        args.report = True

    print("=" * 74)
    print("KQ-Propel synthetic data provenance pipeline")
    print("=" * 74)

    if args.report:
        print("\n[Stage A] Policy corpus provenance manifest")
        prov_problems, prov_stats = audit_provenance_manifest()
        for k, v in prov_stats.items():
            print(f"  {k:.<25} {v}")
        for p in prov_problems:
            print(f"  PROVENANCE GAP: {p}")
        if not prov_problems:
            print("  status ................... every document declares a verified provenance")

        print("\n[Stage A] Ground-truth fee registry")
        problems = audit_fee_registry()
        print(f"  declared figures ......... {len(FEE_REGISTRY)}")
        print(f"  verified against corpus .. {len(FEE_REGISTRY) - len(problems)}")
        for p in problems:
            print(f"  MISMATCH: {p}")
        if not problems:
            print("  status ................... all figures traceable to source document")

        print("\n[Stage A] Evaluation set validation")
        eval_problems, eval_stats = audit_evaluation_set()
        for k, v in eval_stats.items():
            print(f"  {k:.<25} {v}")
        for p in eval_problems:
            print(f"  UNANSWERABLE: {p}")
        if not eval_problems:
            print("  status ................... every expected answer is present in its source")

        print("\n[Stage B] Shipped frustration dataset")
        shipped = load_shipped()
        for k, v in describe(shipped, "frustration_dataset.csv").items():
            print(f"  {k:.<25} {v}")

        print("\n[Stage C] Retraining on shipped dataset")
        try:
            metrics = train_and_score(shipped)
            for k, v in metrics.items():
                print(f"  {k:.<25} {v}")
        except ImportError as exc:
            print(f"  skipped (missing dependency: {exc.name})")

    if args.regenerate:
        pool = theoretical_pool_size()
        print(f"\n[Stage B] Construction grammar capacity")
        print(f"  frustrated templates ..... {len(FRUSTRATED_TEMPLATES)}")
        print(f"  calm templates ........... {len(CALM_TEMPLATES)}")
        print(f"  distinct frustrated ...... {pool['frustrated']}")
        print(f"  distinct calm ............ {pool['calm']}")
        print(f"  sampled per class ........ {args.per_class} (seed={args.seed})")

        rows = generate_frustration_dataset(args.seed, args.per_class)
        print(f"\n[Stage B] Regenerated dataset")
        for k, v in describe(rows, "regenerated").items():
            print(f"  {k:.<25} {v}")

        target = SENTIMENT_CSV if args.overwrite else OUT_DIR / "frustration_dataset.csv"
        write_csv(rows, target)
        print(f"  written to ............... {target}")

        print("\n[Stage C] Training on regenerated dataset")
        try:
            metrics = train_and_score(rows)
            for k, v in metrics.items():
                print(f"  {k:.<25} {v}")
            (OUT_DIR / "regenerated_metrics.json").write_text(
                json.dumps(metrics, indent=2), encoding="utf-8")
        except ImportError as exc:
            print(f"  skipped (missing dependency: {exc.name})")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
