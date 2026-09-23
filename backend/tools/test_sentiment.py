"""Regression tests for the frustration classifier.

These exist because the classifier once reported 0.96 accuracy while calling
"I am furious, nobody has helped me at all" calm. The headline number was
real but meaningless: no calm row in the training file contained a question
mark, so the model learned punctuation instead of frustration, and a held-out
split of that same file could not detect it.

The tests below therefore check the properties the accuracy figure missed:
that unambiguous sentences are classified correctly, that punctuation alone
cannot flip a prediction, that the empathy gate fires only where intended,
and that the challenge set stays out of the training data.
"""
import csv
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, sentiment  # noqa: E402

DATASET = config.SENTIMENT_DATASET_PATH
CHALLENGE = DATASET.with_name("frustration_challenge.csv")


def score(text: str) -> float:
    return sentiment.classify_frustration(text)["frustration_score"]


def label(text: str) -> str:
    return sentiment.classify_frustration(text)["label"]


class UnambiguousCases(unittest.TestCase):
    """Sentences whose label no reasonable person would dispute."""

    FRUSTRATED = [
        "I am furious, nobody has helped me at all",
        "Worst airline ever, I want my money back now",
        "This is a disgrace, three days and still no bag",
        "I have been ignored for hours and I am done with this",
        "Absolutely unacceptable, I have been waiting since Monday",
        "You have ruined my trip and nobody seems to care",
    ]

    CALM = [
        "How much does it cost to book a ticket by telephone?",
        "Good morning, what is the check-in time for Nairobi?",
        "Could you confirm the cabin baggage size please?",
        "Thanks, and how much is the name change fee?",
        "I would like to add an extra bag to my booking.",
        "Please advise on the documents required for Dubai.",
    ]

    def test_frustrated_sentences_are_flagged(self):
        for text in self.FRUSTRATED:
            with self.subTest(text=text):
                self.assertEqual(label(text), "frustrated")

    def test_calm_sentences_are_not_flagged(self):
        for text in self.CALM:
            with self.subTest(text=text):
                self.assertEqual(label(text), "calm")


class PunctuationInvariance(unittest.TestCase):
    """A question mark is not evidence of anger, in either direction."""

    SENTENCES = [
        "How much does it cost to book a ticket by telephone",
        "What is the baggage allowance in economy class",
        "Can you tell me what time check-in opens",
        "I would like to know the refund policy",
        "How do I pay for extra baggage",
    ]

    def test_question_mark_never_crosses_the_decision_boundary(self):
        for base in self.SENTENCES:
            with self.subTest(base=base):
                without = score(base)
                with_q = score(base + "?")
                self.assertEqual(
                    without >= 0.5, with_q >= 0.5,
                    f"adding '?' changed the label: {without:.3f} -> {with_q:.3f}")

    def test_mean_punctuation_effect_is_small_in_both_directions(self):
        deltas = [score(s + "?") - score(s) for s in self.SENTENCES]
        mean = sum(deltas) / len(deltas)
        # Checked with abs(): the original bug had a positive effect, the
        # first attempted fix over-corrected into a large negative one.
        self.assertLess(
            abs(mean), 0.15,
            f"'?' alone moves the score by {mean:+.3f} on average")


class DatasetIntegrity(unittest.TestCase):

    @staticmethod
    def _rows(path):
        with open(path, newline="", encoding="utf-8") as f:
            return [r for r in csv.DictReader(f) if r.get("text")]

    def test_challenge_set_exists(self):
        self.assertTrue(CHALLENGE.exists(),
                        "the challenge set is the only honest generalisation measure")

    def test_challenge_set_is_not_in_the_training_data(self):
        train = {r["text"].strip().lower() for r in self._rows(DATASET)}
        overlap = [r["text"] for r in self._rows(CHALLENGE)
                   if r["text"].strip().lower() in train]
        self.assertEqual(overlap, [], f"{len(overlap)} challenge rows leaked into training")

    def test_punctuation_is_not_perfectly_separating(self):
        rows = self._rows(DATASET)
        for mark in ("?", "!"):
            for lab in ("calm", "frustrated"):
                sub = [r for r in rows if r["label"] == lab]
                share = sum(mark in r["text"] for r in sub) / len(sub)
                with self.subTest(mark=mark, label=lab):
                    # A class with 0% of a mark that the other class has lets
                    # the model separate on punctuation alone.
                    self.assertGreater(
                        share, 0.0,
                        f"no {lab} row contains '{mark}' - that is the original bug")

    def test_evaluation_probes_are_not_in_the_training_data(self):
        """Probe sentences must not be trainable, or the probe measures
        recall of a memorised row rather than generalisation. Twelve had
        leaked in when this test was written."""
        import re
        probes = set()
        for name in ("diagnose_sentiment.py", "test_sentiment.py"):
            src = (Path(__file__).parent / name).read_text(encoding="utf-8")
            for m in re.finditer(r'"([^"\n]{20,120})"', src):
                probes.add(m.group(1).strip().lower().rstrip("?.!"))
        leaked = [r["text"] for r in self._rows(DATASET)
                  if r["text"].strip().lower().rstrip("?.!") in probes]
        self.assertEqual(leaked, [], f"{len(leaked)} probe sentence(s) are in training data")

    def test_classes_are_roughly_balanced(self):
        rows = self._rows(DATASET)
        calm = sum(r["label"] == "calm" for r in rows)
        self.assertGreater(min(calm, len(rows) - calm) / len(rows), 0.4)


class ReportedMetrics(unittest.TestCase):

    def test_both_numbers_are_reported(self):
        m = sentiment.get_metrics()
        self.assertIn("challenge", m)
        self.assertIn("generalization_gap", m)
        self.assertIn("accuracy", m["challenge"])

    def test_challenge_accuracy_is_not_a_stub(self):
        m = sentiment.get_metrics()
        self.assertEqual(m["challenge"]["size"], 100)
        self.assertGreater(m["challenge"]["accuracy"], 0.6)


class EmpathyGate(unittest.TestCase):
    """The gate is sound; it was the classifier feeding it that was wrong."""

    def test_gate_threshold_is_above_the_decision_boundary(self):
        # 0.55 rather than 0.50, so a marginal call does not trigger an
        # apology to a customer who simply asked a question.
        from app import agent
        self.assertGreaterEqual(agent.EMPATHY_THRESHOLD, 0.55)

    def test_calm_question_gets_no_empathy_prefix(self):
        from app import agent
        body = "Telephone booking costs USD 100."
        result = sentiment.classify_frustration("How much is telephone booking?")
        self.assertEqual(agent._apply_empathy(body, result), body)

    def test_clear_complaint_gets_an_empathy_prefix(self):
        from app import agent
        body = "Baggage claims are handled within 21 days."
        result = sentiment.classify_frustration("I am furious, nobody has helped me at all")
        out = agent._apply_empathy(body, result)
        self.assertNotEqual(out, body)
        self.assertIn(body, out)

    def test_score_just_below_threshold_stays_silent(self):
        from app import agent
        body = "Answer."
        borderline = {"label": "frustrated", "frustration_score": 0.54}
        self.assertEqual(agent._apply_empathy(body, borderline), body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
