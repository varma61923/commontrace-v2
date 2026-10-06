# Generic judge reproducing CommonTrace's initial conversation benchmark judge.
# Reproduces JUDGE_PROMPT and grading logic from benchmarks/conversation_bench.py verbatim.
from __future__ import annotations

from typing import Any, Callable

DEFAULT_MODEL = "claude-sonnet-5"

JUDGE_PROMPT = """Grade an answer against a gold answer. Be generous: the answer is CORRECT if it
contains the same information as the gold answer, even if phrased differently or longer.
For time questions, the same date or period in another format is CORRECT.

Question: {question}
Gold answer: {gold}
Answer: {answer}

Reply with exactly one word: CORRECT or WRONG."""


def format_prompt(question: str, gold: str, answer: str) -> str:
    """Format the generic judge prompt comparing a candidate answer to a gold answer."""
    return JUDGE_PROMPT.format(question=question, gold=gold, answer=answer.strip())


def parse_response(response: str) -> bool:
    """Parse the judge response text. Returns True if judged CORRECT, False otherwise."""
    if not response:
        return False
    return response.strip().upper().startswith("CORRECT")


def score(response: str) -> float:
    """Score the judge response text on a [0.0, 1.0] scale."""
    return 1.0 if parse_response(response) else 0.0


class GenericJudge:
    """Generic benchmark judge reproducing CommonTrace's baseline judge logic."""

    name: str = "generic"
    default_model: str = DEFAULT_MODEL
    prompt_template: str = JUDGE_PROMPT

    def __init__(self, model: str | None = None):
        self.model = model or self.default_model

    def format_prompt(self, question: str, gold: str, answer: str) -> str:
        return format_prompt(question=question, gold=gold, answer=answer)

    def parse_response(self, response: str) -> bool:
        return parse_response(response)

    def score(self, response: str) -> float:
        return score(response)

    def grade(
        self,
        question: dict[str, Any] | str,
        answer: str,
        *,
        complete_fn: Callable[[str], tuple[str, dict]] | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Grade a candidate answer against a question or question dictionary.

        If question is a dict, expects 'question' and 'answer' keys.
        """
        if isinstance(question, dict):
            q_text = question.get("question", "")
            gold_text = str(question.get("answer", ""))
        else:
            q_text = str(question)
            gold_text = str(kwargs.get("gold", ""))

        prompt = self.format_prompt(question=q_text, gold=gold_text, answer=answer)

        if complete_fn is None:
            from commontrace import llm

            cfg = llm.Config(
                provider="anthropic",
                model=model or self.model,
                api_key=llm.load_config().api_key if hasattr(llm, "load_config") else "",
            ) if model else None
            raw_response, usage = llm.complete(prompt, config=cfg)
        else:
            raw_response, usage = complete_fn(prompt)

        is_correct = self.parse_response(raw_response)
        numeric_score = 1.0 if is_correct else 0.0

        return {
            "answer": answer.strip(),
            "correct": is_correct,
            "score": numeric_score,
            "raw_response": raw_response.strip(),
            "model": model or self.model,
            "usage": usage,
        }
