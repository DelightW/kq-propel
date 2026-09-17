"""Builds the Colab-ready notebook from the generation pipeline. Run once."""
import json
import pathlib

def md(s):
    return {"cell_type": "markdown", "metadata": {}, "source": s.splitlines(keepends=True)}

def code(s):
    return {"cell_type": "code", "metadata": {}, "execution_count": None,
            "outputs": [], "source": s.strip().splitlines(keepends=True)}

cells = [
md("""# KQ-Propel — Synthetic Data Generation & Provenance

**Reproducible record of how every dataset in this project was constructed.**

The system is grounded on a *synthetic* corpus by design. Real airline policy documents are
copyrighted and their fee schedules change without notice, which makes them unsuitable as a
reproducible academic ground truth. Authoring the corpus means the correct answer to every
evaluation question is known exactly — which is what makes **groundedness measurable** rather
than subjective.

| Stage | Purpose |
|---|---|
| A | Ground-truth fee registry — every figure traced to its source document |
| B | Construction grammar — frustration dataset expanded under a fixed seed |
| C | Train & evaluate — mirrors `backend/app/sentiment.py` exactly |

Run each cell in order.
"""),

md("""## Setup

**On Colab**, run the cell below — it clones the repository and moves into it.
**Locally**, the clone is skipped automatically and your working copy is used."""),
code("""
import os, sys, subprocess
from pathlib import Path

REPO_URL  = "https://github.com/DelightW/kq-propel.git"
REPO_NAME = "kq-propel"

IN_COLAB = "google.colab" in sys.modules
if IN_COLAB and not Path(REPO_NAME).exists():
    subprocess.run(["git", "clone", "-q", REPO_URL, REPO_NAME], check=True)

if IN_COLAB:
    os.chdir(f"/content/{REPO_NAME}/docgen")

sys.path.insert(0, str(Path.cwd()))

try:
    import sklearn, joblib, pandas
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "-q",
                    "scikit-learn", "joblib", "pandas"], check=True)

import pandas as pd
from generate_datasets import BACKEND

print("running in Colab:", IN_COLAB)
print("working dir:", Path.cwd())
print("backend resolved to:", BACKEND)
"""),

md("""## Stage A — Ground-truth fee registry

A single declarative schedule of every monetary value in the domain. The policy corpus and the
evaluation set are both validated against it, so a figure can never silently drift between the
document a passenger is quoted and the key the evaluator expects."""),
code("""
from generate_datasets import FEE_REGISTRY, audit_fee_registry

df = pd.DataFrame(FEE_REGISTRY).T.reset_index().rename(columns={"index": "key"})
display(df)

problems = audit_fee_registry()
print(f"declared: {len(FEE_REGISTRY)}   verified: {len(FEE_REGISTRY) - len(problems)}")
for p in problems:
    print("MISMATCH:", p)
if not problems:
    print("PASS — every figure is traceable to its source document")
"""),

md("""### Evaluation-set validation

Every `expected_keyword` must be answerable from the documents listed in `expected_topics`.
This is what guarantees the evaluation set is not asking unanswerable questions."""),
code("""
from generate_datasets import audit_evaluation_set

problems, stats = audit_evaluation_set()
for k, v in stats.items():
    print(f"{k:.<28} {v}")
for p in problems:
    print("UNANSWERABLE:", p)
if not problems:
    print("PASS — every expected answer is present in its source")
"""),

md("""## Stage B — The construction grammar

The frustrated class is **not** free-form invention. It is built from four pragmatic markers
documented in the customer-service literature:

1. **Repetition of failure** — *"this is the third time..."*
2. **Lexical intensification** — *"absolutely furious", "completely unacceptable"*
3. **Time-cost complaint** — *"on hold for two hours"*
4. **Escalation demand** — *"I want a supervisor immediately"*

The calm class is matched for **topic and length**, so the classifier cannot separate the classes
on subject matter alone — it must learn affect."""),
code("""
from generate_datasets import (FRUSTRATED_TEMPLATES, CALM_TEMPLATES,
                               theoretical_pool_size)

print("FRUSTRATED templates:")
for t in FRUSTRATED_TEMPLATES:
    print("  ", t)
print("\\nCALM templates:")
for t in CALM_TEMPLATES:
    print("  ", t)

pool = theoretical_pool_size()
print("\\nDistinct utterances the grammar can produce:")
print(f"  frustrated: {pool['frustrated']}")
print(f"  calm:       {pool['calm']}")
print(f"  total:      {pool['frustrated'] + pool['calm']}")
"""),

md("### Deterministic expansion\n\nFixed seed → byte-identical output. Any number reported in the dissertation can be regenerated."),
code("""
from generate_datasets import generate_frustration_dataset, describe

rows = generate_frustration_dataset(seed=42, per_class=50)
pd.DataFrame(rows).head(10)
"""),
code("""
again = generate_frustration_dataset(seed=42, per_class=50)
print("reproducible:", rows == again)

for k, v in describe(rows, "regenerated").items():
    print(f"{k:.<24} {v}")
"""),

md("""## Stage C — Train & evaluate

Replicates `backend/app/sentiment.py` exactly: TF-IDF (1–2 grams, sublinear) unioned with nine
hand-engineered **stylistic features** (caps ratio, elongation, punctuation bursts, intensifier
count…) feeding Logistic Regression with balanced class weights. Stratified 75/25,
`random_state=42`.

The stylistic union matters because TF-IDF lower-cases its input, discarding exactly the signal
that separates an angry passenger from a calm one — shouting and punctuation."""),
code("""
from generate_datasets import train_and_score, load_shipped

shipped = load_shipped()
m_shipped = train_and_score(shipped)
m_generated = train_and_score(rows)

pd.DataFrame({"curated (shipped)": m_shipped, "fully templated": m_generated})
"""),

md("""### Reading this result honestly

The fully-templated pool scores **higher** than the curated dataset. That is not a better model —
it is a **known artefact of synthetic data**: purely templated text is trivially separable because
every frustrated item contains a marker drawn from a closed vocabulary.

This is precisely why the shipped dataset is the *human-reviewed* release — templated candidates
were pruned and varied so the task is non-trivial. The **0.96 accuracy on the curated set is the
honest figure to report**; the templated 1.00 is a ceiling effect, not genuine generalisation.

**Stated limitation:** these metrics are on in-domain synthetic text and will degrade on real
passenger language. Validation against a real support-transcript corpus is future work."""),

md("## Export\n\nWrites to `docgen/generated/` — non-destructive by default."),
code("""
from generate_datasets import write_csv, OUT_DIR
import json

write_csv(rows, OUT_DIR / "frustration_dataset.csv")
(OUT_DIR / "regenerated_metrics.json").write_text(json.dumps(m_generated, indent=2))
print("written to", OUT_DIR)
"""),
]

nb = {
    "cells": cells,
    "metadata": {
        "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
        "language_info": {"name": "python", "version": "3.11"},
        "colab": {"provenance": []},
    },
    "nbformat": 4,
    "nbformat_minor": 5,
}

out = pathlib.Path(__file__).parent / "KQ_Propel_Data_Generation.ipynb"
out.write_text(json.dumps(nb, indent=1), encoding="utf-8")
print("wrote", out.resolve())
