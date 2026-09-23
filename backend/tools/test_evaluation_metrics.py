"""
Regression tests for the RAG-Triad metrics.

Every case here is an adversarial example that the previous overlap-based
implementation got wrong. They are kept so the failures cannot silently return.

Run:  python tools/test_evaluation_metrics.py      (no pytest required)
      pytest tools/test_evaluation_metrics.py      (also works)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import evaluation  # noqa: E402

SOURCE = "Bags between 11kg and 20kg over the limit incur a flat fee of Ksh 9,000."


def test_groundedness_accepts_verbatim_extract():
    assert evaluation.groundedness(SOURCE, SOURCE) >= 0.9


def test_groundedness_accepts_correct_paraphrase():
    # Previously scored 0.0 - novel wording, but the quantity agrees.
    answer = "You will pay nine thousand shillings for that bag."
    assert evaluation.groundedness(answer, SOURCE) >= 0.7


def test_groundedness_rejects_fabricated_figure():
    # Previously scored 1.0 - source says 9,000, answer invents 12,000.
    answer = "The fee is Ksh 12,000 payable at the gate by card."
    assert evaluation.groundedness(answer, SOURCE) < 0.3


def test_groundedness_rejects_contradiction():
    # Previously scored 1.0 - denies what the source asserts.
    answer = "There is no fee for bags over the limit."
    assert evaluation.groundedness(answer, SOURCE) < 0.3


def test_groundedness_empty_answer_is_zero():
    assert evaluation.groundedness("", SOURCE) == 0.0


def test_groundedness_ignores_empathy_preamble():
    # The agent prefixes an empathy line for frustrated passengers. It asserts
    # nothing checkable and must not be counted as a hallucination.
    answer = ("I'm really sorry for the trouble - I understand how frustrating "
              "this is, and I'll help you sort it out right away. " + SOURCE)
    assert evaluation.groundedness(answer, SOURCE) >= 0.9


def test_groundedness_still_catches_fabrication_after_empathy():
    answer = ("I'm really sorry for the trouble, I'll help you right away. "
              "The fee is Ksh 12,000 payable at the gate.")
    assert evaluation.groundedness(answer, SOURCE) < 0.3


def test_context_relevance_is_bounded():
    # Previously returned 1.441 by averaging the unbounded ranking score.
    chunks = [{"text": SOURCE, "score": 1.441, "dense_score": 0.83},
              {"text": SOURCE, "score": 0.61, "dense_score": 0.44}]
    value = evaluation.context_relevance(chunks)
    assert 0.0 <= value <= 1.0


def test_context_relevance_clamps_legacy_chunks():
    chunks = [{"text": SOURCE, "score": 1.441}]  # no dense_score
    assert 0.0 <= evaluation.context_relevance(chunks) <= 1.0


def test_context_relevance_empty_is_zero():
    assert evaluation.context_relevance([]) == 0.0


QUESTION = "How much is the overweight baggage fee for a bag that is 15kg over the limit?"


def test_answer_relevance_rewards_the_answer_over_the_echo():
    # Previously the echo scored 1.00 and the answer 0.50.
    answer = evaluation.answer_relevance(SOURCE, QUESTION, ["9,000"])
    echo = evaluation.answer_relevance(QUESTION, QUESTION, ["9,000"])
    assert answer > echo
    assert echo == 0.0


def test_answer_relevance_matches_numeric_variants():
    assert evaluation.answer_relevance("The fee is 9000 shillings.",
                                       QUESTION, ["9,000"]) == 1.0


def test_answer_relevance_fallback_penalises_echo():
    # No gold keys available (live chat path): echo must still lose.
    assert evaluation.answer_relevance(QUESTION, QUESTION) == 0.0
    assert evaluation.answer_relevance(SOURCE, QUESTION) > 0.0


def test_evaluate_response_shape():
    chunks = [{"text": SOURCE, "section": "Section 2", "source": "baggage_policy",
               "score": 1.2, "dense_score": 0.8}]
    result = evaluation.evaluate_response(QUESTION, SOURCE, chunks, ["9,000"])
    assert set(result) == {"context_relevance", "groundedness", "answer_relevance"}
    assert all(0.0 <= v <= 1.0 for v in result.values())


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {name}  {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
