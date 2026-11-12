"""
Passenger sentiment / frustration classifier.

Per the corrections document, this is a machine-learning component that is
trained and evaluated (rather than a wrapper around a pre-trained AI
service): a TF-IDF vectorizer feeding a Logistic Regression classifier,
trained on a labelled dataset of passenger messages, and evaluated with
accuracy, precision, recall and F1-score. Its output (frustration level) is
consumed by the ReAct agent to decide how to respond / escalate.

TF-IDF alone lower-cases its input, which discards exactly the signals that
distinguish an angry passenger from a calm one - shouting in capitals and
bursts of punctuation ("I HAVE WAITED FOR 2 HOURS?!"). The feature space
therefore unions the lexical TF-IDF vectors with hand-engineered stylistic
features that preserve those cues.
"""
import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Dict, List
import joblib
import numpy as np
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (accuracy_score, confusion_matrix, f1_score, precision_recall_fscore_support,
                             precision_score, recall_score)
from sklearn.model_selection import train_test_split
from sklearn.pipeline import FeatureUnion, Pipeline
from sklearn.preprocessing import StandardScaler

from app import config

_METRICS_PATH = config.SENTIMENT_MODEL_PATH.with_suffix(".metrics.json")
_CHALLENGE_PATH = config.SENTIMENT_DATASET_PATH.with_name("frustration_challenge.csv")

# Bumped whenever the feature space or dataset changes, so a stale cached
# model is retrained instead of silently reused.
MODEL_VERSION = 4  # 4: metrics report each class and the confusion counts

_CAPS_WORD_RE = re.compile(r"\b[A-Z]{3,}\b")
_ELONGATED_RE = re.compile(r"([a-zA-Z])\1{2,}")
_INTENSIFIERS = (
    "absolutely", "completely", "totally", "utterly", "ridiculous", "unacceptable",
    "furious", "disgusted", "disgrace", "appalling", "shameful", "worst", "terrible",
    "awful", "nightmare", "disaster", "useless", "incompetence", "angry", "upset",
    "fed up", "sick and tired", "never again", "immediately", "right now", "demand",
    "escalate", "manager", "supervisor", "seriously", "enough is enough",
)


class StyleFeatures(BaseEstimator, TransformerMixin):
    """Hand-engineered stylistic features capturing *how* something was said,
    which survives the lower-casing performed by the TF-IDF stage."""

    feature_names = (
        "caps_ratio", "caps_words", "exclamations", "questions",
        "mixed_punct", "repeated_punct", "elongated", "intensifiers", "length",
    )

    def fit(self, X, y=None):
        return self

    def transform(self, X) -> np.ndarray:
        return np.array([self._features(t) for t in X], dtype=float)

    @staticmethod
    def _features(text: str) -> List[float]:
        text = text or ""
        letters = [c for c in text if c.isalpha()]
        caps_ratio = (sum(1 for c in letters if c.isupper()) / len(letters)) if letters else 0.0
        lowered = text.lower()
        return [
            caps_ratio,
            min(len(_CAPS_WORD_RE.findall(text)), 10) / 10.0,
            min(text.count("!"), 5) / 5.0,
            min(text.count("?"), 5) / 5.0,
            1.0 if ("?!" in text or "!?" in text) else 0.0,
            1.0 if ("!!" in text or "??" in text) else 0.0,
            1.0 if _ELONGATED_RE.search(text) else 0.0,
            min(sum(1 for w in _INTENSIFIERS if w in lowered), 5) / 5.0,
            min(len(text), 200) / 200.0,
        ]


def _build_pipeline() -> Pipeline:
    return Pipeline([
        ("features", FeatureUnion([
            ("tfidf", TfidfVectorizer(ngram_range=(1, 2), min_df=1, sublinear_tf=True)),
            ("style", Pipeline([("raw", StyleFeatures()), ("scale", StandardScaler())])),
        ])),
        ("clf", LogisticRegression(max_iter=2000, class_weight="balanced")),
    ])


def _dataset_fingerprint() -> str:
    digest = hashlib.sha256(config.SENTIMENT_DATASET_PATH.read_bytes()).hexdigest()[:16]
    challenge = ""
    if _CHALLENGE_PATH.exists():
        challenge = hashlib.sha256(_CHALLENGE_PATH.read_bytes()).hexdigest()[:8]
    return f"v{MODEL_VERSION}-{digest}-{challenge}"


def _read_csv(path: Path):
    texts, labels = [], []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not row.get("text"):
                continue
            texts.append(row["text"])
            labels.append(row["label"])
    return texts, labels


def _load_dataset():
    return _read_csv(config.SENTIMENT_DATASET_PATH)


def _score(pipeline, texts, labels) -> Dict:
    pred = pipeline.predict(texts)
    return {
        "accuracy": round(float(accuracy_score(labels, pred)), 4),
        "precision": round(float(precision_score(labels, pred, pos_label="frustrated",
                                                  zero_division=0)), 4),
        "recall": round(float(recall_score(labels, pred, pos_label="frustrated",
                                            zero_division=0)), 4),
        "f1_score": round(float(f1_score(labels, pred, pos_label="frustrated",
                                          zero_division=0)), 4),
        "size": len(texts),
        # The headline figures above are for the frustrated class only. A
        # model can score well there while misreading calm passengers, so
        # both classes and the two kinds of error are reported.
        "per_class": _per_class(labels, pred),
    }


CLASSES = ("calm", "frustrated")


def _per_class(labels, pred) -> Dict:
    p, r, f, n = precision_recall_fscore_support(labels, pred, labels=list(CLASSES), zero_division=0)
    out = {c: {"precision": round(float(p[i]), 4), "recall": round(float(r[i]), 4),
               "f1_score": round(float(f[i]), 4), "support": int(n[i])} for i, c in enumerate(CLASSES)}
    cm = confusion_matrix(labels, pred, labels=list(CLASSES))
    out["errors"] = {"calm_read_as_frustrated": int(cm[0][1]),
                     "frustrated_read_as_calm": int(cm[1][0])}
    return out


def train_frustration_classifier(force: bool = False) -> Dict:
    fingerprint = _dataset_fingerprint()
    if config.SENTIMENT_MODEL_PATH.exists() and _METRICS_PATH.exists() and not force:
        cached = json.loads(_METRICS_PATH.read_text(encoding="utf-8"))
        if cached.get("fingerprint") == fingerprint:
            return cached

    texts, labels = _load_dataset()
    x_train, x_test, y_train, y_test = train_test_split(
        texts, labels, test_size=0.25, random_state=42, stratify=labels
    )

    pipeline = _build_pipeline()
    pipeline.fit(x_train, y_train)

    held_out = _score(pipeline, x_test, y_test)
    metrics = dict(held_out)
    metrics.pop("size", None)
    metrics["train_size"] = len(x_train)
    metrics["test_size"] = len(x_test)
    metrics["fingerprint"] = fingerprint

    # A held-out split of one synthetic file measures how well the model
    # learned that file, including any artifact the file happens to contain.
    # The challenge set is written separately, with different vocabulary, and
    # is never trained on - so the gap between these two numbers is the
    # honest estimate of how much of the headline score is memorisation.
    if _CHALLENGE_PATH.exists():
        c_texts, c_labels = _read_csv(_CHALLENGE_PATH)
        metrics["challenge"] = _score(pipeline, c_texts, c_labels)
        metrics["generalization_gap"] = round(
            held_out["accuracy"] - metrics["challenge"]["accuracy"], 4)

    config.SENTIMENT_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(pipeline, config.SENTIMENT_MODEL_PATH)
    _METRICS_PATH.write_text(json.dumps(metrics, indent=2), encoding="utf-8")

    global _model_cache
    _model_cache = pipeline
    return metrics


def get_metrics() -> Dict:
    if not _METRICS_PATH.exists():
        return train_frustration_classifier()
    metrics = json.loads(_METRICS_PATH.read_text(encoding="utf-8"))
    if metrics.get("fingerprint") != _dataset_fingerprint():
        return train_frustration_classifier(force=True)
    return metrics


_model_cache = None


def _get_model():
    global _model_cache
    if _model_cache is None:
        train_frustration_classifier()
        if _model_cache is None:
            _model_cache = joblib.load(config.SENTIMENT_MODEL_PATH)
    return _model_cache


def classify_frustration(message: str) -> Dict:
    model = _get_model()
    proba = model.predict_proba([message])[0]
    classes = list(model.classes_)
    frustrated_idx = classes.index("frustrated") if "frustrated" in classes else None
    frustration_score = float(proba[frustrated_idx]) if frustrated_idx is not None else 0.0
    # One or two plain words ("yes", "card", a phone number) carry no tone, but
    # the model scored them 0.77-0.86 frustrated, so every "yes" in a payment
    # conversation was answered with an apology. Without a visible sign of
    # emotion (exclamation marks or shouting) such a reply is treated as calm.
    if _too_short_to_judge(message):
        frustration_score = min(frustration_score, SHORT_REPLY_CEILING)
    # A single plain question ("Which routes have a one-piece allowance?",
    # "How long does a refund take?") was scored 0.56 frustrated and answered
    # with an apology. With no exclamation, no shouting and no complaint words
    # it is a request for information, and is kept below the empathy threshold.
    elif _plain_question(message):
        frustration_score = min(frustration_score, PLAIN_QUESTION_CEILING)
    label = "frustrated" if frustration_score >= 0.5 else "calm"
    return {
        "label": label,
        "confidence": round(max(frustration_score, 1 - frustration_score), 3),
        "frustration_score": round(frustration_score, 3),
    }


SHORT_REPLY_CEILING = 0.3
PLAIN_QUESTION_CEILING = 0.45

# "Why" is left out: "why is my bag still not here?" is usually a complaint.
_PLAIN_START_RE = re.compile(r"^(how|what|which|when|where|who|can|could|may|do|does|is|are|will|would|should)\b",
                             re.I)
_COMPLAINT_RE = re.compile(
    r"\b(still|never|nobody|no\s+one|useless|ridiculous|terrible|awful|horrible|worst|unacceptable|"
    r"angry|furious|annoy\w*|frustrat\w*|disappoint\w*|upset|fed\s+up|again|waiting|waited|wait|"
    r"ages|forever|seriously|joke|scam|rubbish|nonsense|stupid|hopeless|disgust\w*|complain\w*|"
    r"lost|missing|damaged|broken|stolen|cancell?ed|rude|ignor\w*|refus\w*|"
    r"not\s+(arrived|received|working|helpful|happy)|hasn'?t|haven'?t|didn'?t|won'?t|can'?t)\b",
    re.I)


def _plain_question(message: str) -> bool:
    text = (message or "").strip()
    if not text.endswith("?") or text.count("?") > 1 or "!" in text:
        return False
    if re.search(r"[.;]\s+\S", text[:-1]):        # more than one sentence
        return False
    words = re.findall(r"[A-Za-z']+", text)
    if any(len(w) >= 3 and w.isupper() for w in words if w not in ("KQ", "USD", "KES")):
        return False
    return bool(_PLAIN_START_RE.match(text)) and not _COMPLAINT_RE.search(text)


def _too_short_to_judge(message: str) -> bool:
    words = re.findall(r"[A-Za-z0-9']+", message or "")
    if len(words) >= 3:
        return False
    shouting = any(len(w) >= 3 and w.isupper() and w.isalpha() for w in words)
    return "!" not in message and not shouting
