"""
Passenger sentiment / frustration classifier.

Per the corrections document, this is a machine-learning component that is
trained and evaluated (rather than a wrapper around a pre-trained AI
service): a TF-IDF vectorizer feeding a Logistic Regression classifier,
trained on a labelled dataset of passenger messages, and evaluated with
accuracy, precision, recall and F1-score. Its output (frustration level) is
consumed by the ReAct agent to decide how to respond / escalate.
"""
import csv
import json
from pathlib import Path
from typing import Dict

import joblib
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline

from app import config

_METRICS_PATH = config.SENTIMENT_MODEL_PATH.with_suffix(".metrics.json")


def _load_dataset():
    texts, labels = [], []
    with open(config.SENTIMENT_DATASET_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            texts.append(row["text"])
            labels.append(row["label"])
    return texts, labels


def train_frustration_classifier(force: bool = False) -> Dict:
    if config.SENTIMENT_MODEL_PATH.exists() and _METRICS_PATH.exists() and not force:
        return json.loads(_METRICS_PATH.read_text(encoding="utf-8"))

    texts, labels = _load_dataset()
    x_train, x_test, y_train, y_test = train_test_split(
        texts, labels, test_size=0.25, random_state=42, stratify=labels
    )

    pipeline = Pipeline([
        ("tfidf", TfidfVectorizer(ngram_range=(1, 2), min_df=1)),
        ("clf", LogisticRegression(max_iter=1000)),
    ])
    pipeline.fit(x_train, y_train)

    y_pred = pipeline.predict(x_test)
    metrics = {
        "accuracy": accuracy_score(y_test, y_pred),
        "precision": precision_score(y_test, y_pred, pos_label="frustrated", zero_division=0),
        "recall": recall_score(y_test, y_pred, pos_label="frustrated", zero_division=0),
        "f1_score": f1_score(y_test, y_pred, pos_label="frustrated", zero_division=0),
        "train_size": len(x_train),
        "test_size": len(x_test),
    }

    config.SENTIMENT_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(pipeline, config.SENTIMENT_MODEL_PATH)
    _METRICS_PATH.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    return metrics


def get_metrics() -> Dict:
    if not _METRICS_PATH.exists():
        return train_frustration_classifier()
    return json.loads(_METRICS_PATH.read_text(encoding="utf-8"))


_model_cache = None


def _get_model():
    global _model_cache
    if _model_cache is None:
        if not config.SENTIMENT_MODEL_PATH.exists():
            train_frustration_classifier()
        _model_cache = joblib.load(config.SENTIMENT_MODEL_PATH)
    return _model_cache


def classify_frustration(message: str) -> Dict:
    model = _get_model()
    label = model.predict([message])[0]
    proba = model.predict_proba([message])[0]
    classes = list(model.classes_)
    confidence = float(max(proba))
    frustrated_idx = classes.index("frustrated") if "frustrated" in classes else None
    frustration_score = float(proba[frustrated_idx]) if frustrated_idx is not None else 0.0
    return {
        "label": label,
        "confidence": round(confidence, 3),
        "frustration_score": round(frustration_score, 3),
    }
