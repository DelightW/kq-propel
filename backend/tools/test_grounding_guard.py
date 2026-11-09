"""Regression tests for the grounding guard and route-aware ranking.

The guard decides whether the agent answers from the retrieved policy text or
declines. It has two opposite failure modes, and both were observed in the
functional tests of the project documentation (TC10 to TC12):

  * refusing an answerable question  - a frustrated passenger asking what they
    can claim for a missing bag was told the policy had nothing on it;
  * answering an unanswerable one    - "What is the weather in Nairobi?" was
    answered from a baggage section that happened to mention Nairobi.

Every evaluation question must pass the guard and every off-topic probe must be
refused, so a threshold change that fixes one side cannot silently break the
other. Runs offline; it does not touch the application database.

Run:  python tools/test_grounding_guard.py
"""
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app import agent, config, retrieval  # noqa: E402
from app.vectorstore import get_vector_store  # noqa: E402

passed, failed = [], []


def check(name, condition, detail=""):
    (passed if condition else failed).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name + (f" :: {detail}" if detail and not condition else ""))


def guard(message: str) -> bool:
    """Mirrors the agent: retrieve on the focused request, then check both the
    focused request and, when it differs, the whole message."""
    query = retrieval.focus_query(message)
    chunks = get_vector_store().similarity_search(query, k=4)
    return agent._is_grounded_in_context(query, chunks) or (
        query != message and agent._is_grounded_in_context(message, chunks))


ANSWERABLE_EXTRA = [
    "My bag has been missing for three days and NOBODY is helping me!! What can I claim?",
    "This is RIDICULOUS!! I have waited weeks. How long does a refund take?",
    "I am so angry, my flight was cancelled again!! Can I get a refund?",
    "How many bags can I check in Economy on a flight from Nairobi to London?",
    "How much does it cost to correct my name on an international booking?",
    "Can I take my golf clubs to Dubai?",
    "What is the hand luggage limit in Business class?",
    "when does online check in close",
    "can i pay with mpesa",
]

OFF_TOPIC = [
    "What is the weather in Nairobi?",
    "What is the weather like in London today?",
    "Recommend a good restaurant in Mombasa",
    "What is the capital of France?",
    "Tell me a joke",
    "How do I bake a chocolate cake?",
    "Who won the football match last night?",
    "What time is it in Dubai?",
    "Translate hello into French",
    "What is the exchange rate of the shilling?",
]

print("\n=== Answerable questions pass the guard ===")
dataset = json.loads(config.EVAL_DATASET_PATH.read_text(encoding="utf-8"))
for item in dataset:
    check(f"eval {item['id']}: {item['query'][:60]}", guard(item["query"]))
for q in ANSWERABLE_EXTRA:
    check(f"extra: {q[:60]}", guard(q))

print("\n=== Off-topic questions are refused ===")
for q in OFF_TOPIC:
    check(f"refuse: {q}", not guard(q))

print("\n=== Route-aware ranking ===")
store = get_vector_store()
top = store.similarity_search(
    "How many bags can I check in Economy on a flight from Nairobi to London?", k=4)[0]
check("Nairobi to London ranks the general allowance first",
      top.get("section", "").startswith("Section 1"), top.get("section"))
top = store.similarity_search(
    "How many bags can I check on a domestic flight from Nairobi to Kisumu?", k=4)[0]
check("Nairobi to Kisumu still ranks the one-piece exception first",
      top.get("section", "").startswith("Section 2"), top.get("section"))
top = store.similarity_search("Can I pay with M-Pesa from London?", k=4)
check("M-Pesa from London still retrieves the mobile money section",
      any(r.get("doc_id") == "payment_methods_policy" for r in top))

print(f"\n{len(passed)}/{len(passed) + len(failed)} passed")
sys.exit(1 if failed else 0)
