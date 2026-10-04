# Reproduces LongMemEval official judge prompt and scoring verbatim.
# Source: https://github.com/xiaowu0162/longmemeval/blob/main/src/evaluation/evaluate_qa.py
# Commit: 2ec2a557f339b6c0369619b1ed5793734cc87533
from __future__ import annotations

from typing import Any, Callable

DEFAULT_MODEL = "gpt-4o"

SINGLE_SESSION_PROMPT = (
    "I will give you a question, a correct answer, and a response from a model. "
    "Please answer yes if the response contains the correct answer. Otherwise, answer no. "
    "If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, "
    "you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. "
    "\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
)

TEMPORAL_REASONING_PROMPT = (
    "I will give you a question, a correct answer, and a response from a model. "
    "Please answer yes if the response contains the correct answer. Otherwise, answer no. "
    "If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, "
    "you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. "
    "In addition, do not penalize off-by-one errors for the number of days. If the question asks for the number of days/weeks/months, etc., "
    "and the model makes off-by-one errors (e.g., predicting 19 days when the answer is 18), the model's response is still correct. "
    "\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
)

KNOWLEDGE_UPDATE_PROMPT = (
    "I will give you a question, a correct answer, and a response from a model. "
    "Please answer yes if the response contains the correct answer. Otherwise, answer no. "
    "If the response contains some previous information along with an updated answer, "
    "the response should be considered as correct as long as the updated answer is the required answer."
    "\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
)

SINGLE_SESSION_PREFERENCE_PROMPT = (
    "I will give you a question, a rubric for desired personalized response, and a response from a model. "
    "Please answer yes if the response satisfies the desired response. Otherwise, answer no. "
    "The model does not need to reflect all the points in the rubric. "
    "The response is correct as long as it recalls and utilizes the user's personal information correctly."
    "\n\nQuestion: {}\n\nRubric: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
)

ABSTENTION_PROMPT = (
    "I will give you an unanswerable question, an explanation, and a response from a model. "
    "Please answer yes if the model correctly identifies the question as unanswerable. "
    "The model could say that the information is incomplete, or some other information is given but the asked information is not."
    "\n\nQuestion: {}\n\nExplanation: {}\n\nModel Response: {}\n\n"
    "Does the model correctly identify the question as unanswerable? Answer yes or no only."
)

PROMPT_TEMPLATES: dict[str, str] = {
    "single-session-user": SINGLE_SESSION_PROMPT,
    "single-session-assistant": SINGLE_SESSION_PROMPT,
    "multi-session": SINGLE_SESSION_PROMPT,
    "temporal-reasoning": TEMPORAL_REASONING_PROMPT,
    "knowledge-update": KNOWLEDGE_UPDATE_PROMPT,
    "single-session-preference": SINGLE_SESSION_PREFERENCE_PROMPT,
    "abstention": ABSTENTION_PROMPT,
}


def normalize_task_name(task: str) -> tuple[str, bool]:
    """Normalize task name and detect abstention suffix."""
    task_clean = task.strip().lower().replace("_", "-")
    abstention = False
    if task_clean.endswith("-abstain") or task_clean.endswith("-abs"):
        abstention = True
        task_clean = task_clean.rsplit("-", 1)[0]
    elif task_clean == "abstention":
        abstention = True
    aliases = {
        "temporal": "temporal-reasoning",
        "preference": "single-session-preference",
        "update": "knowledge-update",
    }
    task_clean = aliases.get(task_clean, task_clean)
    return task_clean, abstention


def get_anscheck_prompt(
    task: str,
    question: str,
    answer: str,
    response: str,
    abstention: bool = False,
) -> str:
    """Reproduces get_anscheck_prompt verbatim from LongMemEval evaluate_qa.py."""
    task_norm, task_abs = normalize_task_name(task)
    is_abstention = abstention or task_abs

    if not is_abstention:
        if task in ["single-session-user", "single-session-assistant", "multi-session"] or \
                task_norm in ["single-session-user", "single-session-assistant", "multi-session"]:
            template = SINGLE_SESSION_PROMPT
            return template.format(question, answer, response)
        elif task == "temporal-reasoning" or task_norm == "temporal-reasoning":
            template = TEMPORAL_REASONING_PROMPT
            return template.format(question, answer, response)
        elif task == "knowledge-update" or task_norm == "knowledge-update":
            template = KNOWLEDGE_UPDATE_PROMPT
            return template.format(question, answer, response)
        elif task == "single-session-preference" or task_norm == "single-session-preference":
            template = SINGLE_SESSION_PREFERENCE_PROMPT
            return template.format(question, answer, response)
        else:
            raise NotImplementedError(f"Task '{task}' is not supported in LongMemEval evaluation.")
    else:
        template = ABSTENTION_PROMPT
        return template.format(question, answer, response)


def parse_response(eval_response: str) -> bool:
    """Official LongMemEval scoring rule verbatim: 'yes' in eval_response.lower()."""
    if not eval_response:
        return False
    return "yes" in eval_response.lower()


def score(eval_response: str) -> float:
    """Return numeric score 1.0 (correct) or 0.0 (incorrect)."""
    return 1.0 if parse_response(eval_response) else 0.0


class LongMemEvalJudge:
    """Official LongMemEval benchmark judge."""

    name: str = "longmemeval"
    default_model: str = DEFAULT_MODEL

    def __init__(self, model: str | None = None):
        self.model = model or self.default_model

    def format_prompt(
        self,
        task: str,
        question: str,
        answer: str,
        response: str,
        abstention: bool = False,
    ) -> str:
        return get_anscheck_prompt(
            task=task,
            question=question,
            answer=answer,
            response=response,
            abstention=abstention,
        )

    def parse_response(self, eval_response: str) -> bool:
        return parse_response(eval_response)

    def score(self, eval_response: str) -> float:
        return score(eval_response)

    def grade(
        self,
        question: dict[str, Any] | str,
        answer: str,
        *,
        task: str | None = None,
        complete_fn: Callable[[str], tuple[str, dict]] | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Grade a candidate answer against LongMemEval reference data."""
        if isinstance(question, dict):
            q_text = question.get("question", "")
            gold_text = str(question.get("answer", ""))
            task_type = task or question.get("type") or question.get("question_type", "multi-session")
            q_id = str(question.get("id", question.get("question_id", "")))
            is_abstention = "_abs" in q_id or str(task_type).endswith("-abstain") or str(task_type) == "abstention"
        else:
            q_text = str(question)
            gold_text = str(kwargs.get("gold", ""))
            task_type = task or kwargs.get("type", "multi-session")
            is_abstention = bool(kwargs.get("abstention", False))

        prompt = self.format_prompt(
            task=str(task_type),
            question=q_text,
            answer=gold_text,
            response=answer,
            abstention=is_abstention,
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

        is_correct = self.parse_response(raw_response)
        numeric_score = 1.0 if is_correct else 0.0

        return {
            "answer": answer.strip(),
            "correct": is_correct,
            "score": numeric_score,
            "task": task_type,
            "abstention": is_abstention,
            "raw_response": raw_response.strip(),
            "model": model or self.model,
            "usage": usage,
        }
