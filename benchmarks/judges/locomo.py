# Reproduces LoCoMo official judge prompt and scoring verbatim.
# Source: arXiv 2402.17753 and arXiv 2504.19413 (commit fc49c88243ccccc6950495edfc5cd30af4b72107)
# References: EverOS benchmarks/adapters/locomo.py lines 172-198
from __future__ import annotations

import json
import re
from typing import Any, Callable

DEFAULT_MODEL = "gpt-4o"

LOCOMO_CATEGORIES: dict[int, str] = {
    1: "multi-hop",
    2: "temporal",
    3: "open-domain",
    4: "single-hop",
}

EXCLUDED_CATEGORIES: set[int] = {5}

CATEGORY_NAMES: dict[str, int] = {v: k for k, v in LOCOMO_CATEGORIES.items()}

JUDGE_SYSTEM_PROMPT = "You are an expert grader that determines if answers to questions match a gold standard answer"

JUDGE_USER_PROMPT = """Your task is to label an answer to a question as 'CORRECT' or 'WRONG'. You will be given the following data:
    (1) a question (posed by one user to another user),
    (2) a 'gold' (ground truth) answer,
    (3) a generated answer
which you will score as CORRECT/WRONG.

The point of the question is to ask about something one user should know about the other user based on their prior conversations.
The gold answer will usually be a concise and short answer that includes the referenced topic, for example:
Question: Do you remember what I got the last time I went to Hawaii?
Gold answer: A shell necklace
The generated answer might be much longer, but you should be generous with your grading - as long as it touches on the same topic as the gold answer, it should be counted as CORRECT.

For time related questions, the gold answer will be a specific date, month, year, etc. The generated answer might be much longer or use relative time references (like "last Tuesday" or "next month"), but you should be generous with your grading - as long as it refers to the same date or time period as the gold answer, it should be counted as CORRECT. Even if the format differs (e.g., "May 7th" vs "7 May"), consider it CORRECT if it's the same date.

Now it's time for the real question:
Question: {question}
Gold answer: {golden_answer}
Generated answer: {generated_answer}

First, provide a short (one sentence) explanation of your reasoning, then finish with CORRECT or WRONG.
Do NOT include both CORRECT and WRONG in your response, or it will break the evaluation script.

Just return the label CORRECT or WRONG in a json format with the key as "label".
"""


def is_scorable_category(category: int | str) -> bool:
    """Return True if category is one of official LoCoMo categories 1-4, excluding category 5."""
    if isinstance(category, int):
        return category in LOCOMO_CATEGORIES and category not in EXCLUDED_CATEGORIES
    if isinstance(category, str):
        cat_str = category.strip().lower()
        if cat_str.isdigit():
            val = int(cat_str)
            return val in LOCOMO_CATEGORIES and val not in EXCLUDED_CATEGORIES
        if "5" in cat_str or "adversarial" in cat_str:
            return False
        return cat_str in CATEGORY_NAMES or cat_str in LOCOMO_CATEGORIES.values()
    return False


def format_user_prompt(question: str, golden_answer: str, generated_answer: str) -> str:
    """Format the LoCoMo user prompt with question, gold answer, and candidate answer."""
    return JUDGE_USER_PROMPT.format(
        question=question,
        golden_answer=golden_answer,
        generated_answer=generated_answer.strip(),
    )


def format_messages(question: str, golden_answer: str, generated_answer: str) -> list[dict[str, str]]:
    """Format messages array for chat completion models (system prompt + user prompt)."""
    return [
        {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": format_user_prompt(question, golden_answer, generated_answer)},
    ]


def format_prompt(question: str, golden_answer: str, generated_answer: str) -> str:
    """Single prompt combining system instructions and user prompt for text completion models."""
    return f"{JUDGE_SYSTEM_PROMPT}\n\n{format_user_prompt(question, golden_answer, generated_answer)}"


def parse_response(raw: str) -> dict[str, Any]:
    """Parse JSON response containing {"label": "CORRECT"|"WRONG"} and optional explanation."""
    if not raw or not raw.strip():
        return {"label": "WRONG", "correct": False, "explanation": "", "raw": raw}

    text = raw.strip()

    # Strip code block fences if present
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()

    parsed_json: dict[str, Any] | None = None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            parsed_json = parsed
    except json.JSONDecodeError:
        obj_match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
        if obj_match:
            try:
                parsed = json.loads(obj_match.group())
                if isinstance(parsed, dict):
                    parsed_json = parsed
            except json.JSONDecodeError:
                pass

    if parsed_json is not None:
        raw_label = str(parsed_json.get("label", "")).strip().upper()
        explanation = str(parsed_json.get("explanation", parsed_json.get("reason", ""))).strip()
        correct = raw_label == "CORRECT"
        return {
            "label": "CORRECT" if correct else ("WRONG" if raw_label == "WRONG" else raw_label),
            "correct": correct,
            "explanation": explanation,
            "raw": raw,
        }

    # Fallback regex search for "label": "CORRECT"|"WRONG"
    match = re.search(r'"label"\s*:\s*"([A-Za-z]+)"', text, re.IGNORECASE)
    if match:
        val = match.group(1).upper()
        correct = val == "CORRECT"
        return {"label": val, "correct": correct, "explanation": "", "raw": raw}

    # Final fallback: text ending or presence
    clean_upper = text.upper()
    if (
        clean_upper.startswith("CORRECT")
        or clean_upper.endswith("CORRECT")
        or "LABEL: CORRECT" in clean_upper
        or '"CORRECT"' in clean_upper
        or "CORRECT" in clean_upper.split()
    ):
        return {"label": "CORRECT", "correct": True, "explanation": "", "raw": raw}

    return {"label": "WRONG", "correct": False, "explanation": "", "raw": raw}


def score(response: str) -> float:
    """Return numeric score 1.0 (CORRECT) or 0.0 (WRONG)."""
    return 1.0 if parse_response(response)["correct"] else 0.0


class LoCoMoJudge:
    """Official LoCoMo benchmark judge."""

    name: str = "locomo"
    default_model: str = DEFAULT_MODEL
    system_prompt: str = JUDGE_SYSTEM_PROMPT
    user_prompt_template: str = JUDGE_USER_PROMPT

    def __init__(self, model: str | None = None):
        self.model = model or self.default_model

    def format_user_prompt(self, question: str, golden_answer: str, generated_answer: str) -> str:
        return format_user_prompt(question, golden_answer, generated_answer)

    def format_messages(self, question: str, golden_answer: str, generated_answer: str) -> list[dict[str, str]]:
        return format_messages(question, golden_answer, generated_answer)

    def format_prompt(self, question: str, golden_answer: str, generated_answer: str) -> str:
        return format_prompt(question, golden_answer, generated_answer)

    def parse_response(self, response: str) -> dict[str, Any]:
        return parse_response(response)

    def score(self, response: str) -> float:
        return score(response)

    def is_scorable(self, category: int | str) -> bool:
        return is_scorable_category(category)

    def grade(
        self,
        question: dict[str, Any] | str,
        answer: str,
        *,
        category: int | str | None = None,
        complete_fn: Callable[[str], tuple[str, dict]] | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Grade a candidate answer against LoCoMo reference data."""
        if isinstance(question, dict):
            q_text = question.get("question", "")
            gold_text = str(question.get("answer", ""))
            cat = category if category is not None else question.get("category", question.get("type"))
        else:
            q_text = str(question)
            gold_text = str(kwargs.get("gold", kwargs.get("golden_answer", "")))
            cat = category if category is not None else kwargs.get("category")

        # Exclude category 5 if specified
        if cat is not None and not is_scorable_category(cat):
            return {
                "answer": answer.strip(),
                "correct": None,
                "score": None,
                "excluded": True,
                "category": cat,
                "model": model or self.model,
            }

        prompt = self.format_prompt(
            question=q_text,
            golden_answer=gold_text,
            generated_answer=answer,
        )

        if complete_fn is None:
            from commontrace import llm

            cfg = llm.Config(
                provider="openai-compatible",
                model=model or self.model,
                api_key=llm.load_config().api_key if hasattr(llm, "load_config") else "",
            ) if model else None
            raw_response, usage = llm.complete(prompt, config=cfg)
        else:
            raw_response, usage = complete_fn(prompt)

        parsed = self.parse_response(raw_response)
        numeric_score = 1.0 if parsed["correct"] else 0.0

        return {
            "answer": answer.strip(),
            "correct": parsed["correct"],
            "score": numeric_score,
            "label": parsed["label"],
            "explanation": parsed.get("explanation", ""),
            "raw_response": raw_response.strip(),
            "category": cat,
            "excluded": False,
            "model": model or self.model,
            "usage": usage,
        }
