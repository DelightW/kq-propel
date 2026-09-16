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
from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import FeatureUnion, Pipeline
from sklearn.preprocessing import StandardScaler

from app import config

_METRICS_PATH = config.SENTIMENT_MODEL_PATH.with_suffix(".metrics.json")

# Bumped whenever the feature space or dataset changes, so a stale cached
# model is retrained instead of silently reused.
MODEL_VERSION = 2

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
    return f"v{MODEL_VERSION}-{digest}"


def _load_dataset():
    texts, labels = [], []
    with open(config.SENTIMENT_DATASET_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row.get("text"):
                continue
            texts.append(row["text"])
            labels.append(row["label"])
    return texts, labels


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

    y_pred = pipeline.predict(x_test)
    metrics = {
        "accuracy": round(float(accuracy_score(y_test, y_pred)), 4),
        "precision": round(float(precision_score(y_test, y_pred, pos_label="frustrated",
                                                  zero_division=0)), 4),
        "recall": round(float(recall_score(y_test, y_pred, pos_label="frustrated",
                                            zero_division=0)), 4),
        "f1_score": round(float(f1_score(y_test, y_pred, pos_label="frustrated",
                                          zero_division=0)), 4),
        "train_size": len(x_train),
        "test_size": len(x_test),
        "fingerprint": fingerprint,
    }

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
    label = "frustrated" if frustration_score >= 0.5 else "calm"
    return {
        "label": label,
        "confidence": round(float(max(proba)), 3),
        "frustration_score": round(frustration_score, 3),
    }
