# Dolphin judge for DolphinBench personal assistant memory evaluations.
from __future__ import annotations

from .generic import GenericJudge

DEFAULT_MODEL = "gpt-4o"

DOLPHIN_JUDGE_PROMPT = """You are evaluating an AI assistant's memory recall on the DolphinBench benchmark.
Compare the assistant's answer against the gold reference fact from the user's dated history.

The answer is CORRECT if:
1. It accurately recalls the specific factual detail asked in the question (person, project, date, preference, or event).
2. It does not introduce contradictory or hallucinated facts for the core question.
3. Extra conversational context or minor rephrasing is acceptable as long as the truth condition holds.

Question: {question}
Gold answer: {gold}
Candidate answer: {answer}

Reply with exactly one word: CORRECT or WRONG."""


class DolphinJudge(GenericJudge):
    """Judge specifically tuned for DolphinBench personal assistant long-term memory."""

    name: str = "dolphin"
    default_model: str = DEFAULT_MODEL
    prompt_template: str = DOLPHIN_JUDGE_PROMPT

    def __init__(self, model: str | None = None):
        super().__init__(model=model or self.default_model)

    def format_prompt(self, question: str, gold: str, answer: str) -> str:
        return self.prompt_template.format(question=question, gold=gold, answer=answer.strip())
