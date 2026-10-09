# Deterministic judge for runs without a judge model (local readers, air-gapped hosts).
# Not comparable to published LLM-judge accuracy: it under-credits paraphrases.
from __future__ import annotations

import re
import string
from collections import Counter
from typing import Any, Callable

F1_THRESHOLD = 0.5
_ARTICLES = re.compile(r"\b(a|an|the)\b")


def normalize(text: str) -> str:
    """SQuAD-style normalization: lowercase, drop punctuation and articles, squeeze spaces."""
    text = str(text).lower()
    text = "".join(ch for ch in text if ch not in set(string.punctuation))
    return " ".join(_ARTICLES.sub(" ", text).split())


def token_f1(answer: str, gold: str) -> float:
    predicted, expected = normalize(answer).split(), normalize(gold).split()
    if not predicted or not expected:
        return float(predicted == expected)
    common = sum((Counter(predicted) & Counter(expected)).values())
    if common == 0:
        return 0.0
    precision, recall = common / len(predicted), common / len(expected)
    return 2 * precision * recall / (precision + recall)


class ExactJudge:
    """CORRECT when an accepted gold answer appears in the answer, or token F1 reaches 0.5."""

    name: str = "exact"
    profile: str = "deterministic-containment-or-token-f1-0.5"
    default_model: str = "none"

    def __init__(self, model: str | None = None):
        self.model = "none"

    def grade(self, question: dict[str, Any] | str, answer: str, *,
              complete_fn: Callable[[str], tuple[str, dict]] | None = None,
              model: str | None = None, **kwargs: Any) -> dict[str, Any]:
        if isinstance(question, dict):
            golds = [str(g) for g in (question.get("answers") or [question.get("answer", "")])]
        else:
            golds = [str(kwargs.get("gold", ""))]
        said = normalize(answer)
        best = max((token_f1(answer, gold) for gold in golds), default=0.0)
        contained = any(normalize(gold) and f" {normalize(gold)} " in f" {said} " for gold in golds)
        correct = contained or best >= F1_THRESHOLD
        return {"answer": answer.strip(), "correct": correct, "score": 1.0 if correct else 0.0,
                "raw_response": f"contained={contained} f1={best:.3f}", "model": "none", "usage": {}}
