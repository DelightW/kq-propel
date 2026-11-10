"""
Release quality gate.

Runs the single-turn evaluation set through the agent's answer path and the
conversation cases (data/eval/conversation_cases.json) through the full agent,
then compares the results with the thresholds below. Exits non-zero if any
threshold is missed or any gating case fails, so it can block a release in CI.

    python tools/quality_gate.py

Runs against a temporary database with no live flight data. The thresholds are
set just below today's measured figures, so a change that makes answers worse
fails the gate; raise them as the system improves.
"""
import json
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402

config.APP_DB_PATH = Path(tempfile.mkdtemp()) / "quality_gate.db"
config.AVIATIONSTACK_API_KEY = ""
config.HANDOVER_ENABLED = True
config.HANDOVER_WEBHOOK_URL = ""

from app import agent, database  # noqa: E402
from tools.measure_answers import measure  # noqa: E402

database.init_db()

CASES_PATH = config.DATA_DIR / "eval" / "conversation_cases.json"

THRESHOLDS = {
    "groundedness": 0.95,
    "answer_relevance": 0.60,
    "any_at_k": 0.95,
}


def run_case(case: dict) -> tuple:
    """(passed, detail) for one conversation case."""
    session = f"gate-{case['id']}"
    result = None
    for message in case["turns"]:
        result = agent.run_agent_turn(session, message)
    expect = case["expect"]
    answer = result["answer"]
    lower = answer.lower()
    problems = []

    for key in expect.get("keywords", []):
        if key.lower() not in lower:
            problems.append(f"missing '{key}'")
    if expect.get("refuse") and result["sources"]:
        problems.append("answered from policy instead of declining")
    if "handover" in expect and bool(result.get("handover")) != expect["handover"]:
        problems.append("handover " + ("not opened" if expect["handover"] else "opened wrongly"))
    if expect.get("flight_lookup"):
        looked_up = [s.get("input") for s in result["trace"] if s.get("tool") == "get_flight_status"]
        if expect["flight_lookup"] not in looked_up:
            problems.append(f"did not look up {expect['flight_lookup']}")
    if expect.get("refuse_or_ask"):
        declined = answer.startswith(agent.OUT_OF_SCOPE_REPLY[:40])
        asked = "?" in answer.split("Source:")[0]
        if not (declined or asked):
            problems.append("answered a vague question without asking what was meant")
    return (not problems, "; ".join(problems) or "ok", answer)


def main() -> int:
    failed = False
    print("Single-turn evaluation set")
    single = measure()
    for metric, minimum in THRESHOLDS.items():
        value = single[metric]
        ok = value >= minimum
        failed |= not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {metric:<18} {value:.3f}  (minimum {minimum})")

    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]
    by_category = {}
    for case in cases:
        by_category.setdefault(case["category"], []).append(case)

    print("\nConversation cases")
    for category, items in by_category.items():
        passed = 0
        for case in items:
            ok, detail, answer = run_case(case)
            passed += ok
            gating = case.get("gate", False)
            if not ok and gating:
                failed = True
            mark = "PASS" if ok else ("FAIL" if gating else "gap ")
            print(f"  {mark}  {case['id']:<4} {category:<13} {detail}")
            if not ok:
                print(f"          answer: {answer[:140].replace(chr(10), ' ')}")
        gate_note = "gating" if any(c.get("gate") for c in items) else "tracked only"
        print(f"        {category}: {passed}/{len(items)} ({gate_note})")

    # Tone must never change entitlement: a frustrated passenger gets the same
    # policy answer as a calm one. Run in its own process so its forced
    # classifier cannot leak into anything above.
    import subprocess
    fairness = subprocess.run([sys.executable, str(Path(__file__).with_name("test_sentiment_fairness.py"))],
                              capture_output=True, text=True)
    summary = (fairness.stdout.strip().splitlines() or ["no output"])[-1]
    print("\nSentiment fairness")
    print(f"  {'PASS' if fairness.returncode == 0 else 'FAIL'}  calm and frustrated passengers get "
          f"the same policy answer ({summary})")
    if fairness.returncode != 0:
        failed = True

    unreviewed = sum(1 for c in cases if not c.get("reviewed"))
    if unreviewed:
        print(f"\nNote: {unreviewed} of {len(cases)} conversation cases are drafts awaiting "
              f"review by Kenya Airways staff.")
    print("\nGATE " + ("FAILED" if failed else "PASSED"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
