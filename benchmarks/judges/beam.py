# Reproduces BEAM official judge prompt and scoring verbatim.
# Source: https://github.com/mohammadtavakoli78/BEAM/blob/main/src/evaluation/compute_metrics.py
# Source: https://github.com/mohammadtavakoli78/BEAM/blob/main/src/prompts.py
# Commit: 85048250af007704feccbb0bc46c6a8240b49a51 (arXiv 2510.27246)
from __future__ import annotations

import json
import re
from itertools import combinations
from typing import Any, Callable

DEFAULT_MODEL = "gpt-4.1-mini"

BEAM_ABILITIES: list[str] = [
    "abstention",
    "contradiction_resolution",
    "event_ordering",
    "information_extraction",
    "instruction_following",
    "knowledge_update",
    "multi_session_reasoning",
    "preference_following",
    "summarization",
    "temporal_reasoning",
]

# Exact prompt verbatim from https://github.com/mohammadtavakoli78/BEAM/blob/main/src/prompts.py
UNIFIED_LLM_JUDGE_BASE_PROMPT = """
You are an expert evaluator tasked with judging whether the LLM's response demonstrates compliance with the specified RUBRIC CRITERION.

## EVALUATION INPUTS
- RUBRIC CRITERION (what to check): <rubric_item>
- RESPONSE TO EVALUATE: <llm_response>

## EVALUATION RUBRIC:
The rubric defines a specific requirement, constraint, or expected behavior that the LLM response should demonstrate.

**IMPORTANT**: Pay careful attention to whether the rubric specifies:
- **Positive requirements** (things the response SHOULD include/do)
- **Negative constraints** (things the response SHOULD NOT include/do, often indicated by "no", "not", "avoid", "absent")

## RESPONSIVENESS REQUIREMENT
A compliant response must be **on-topic** and attempt to answer it.
- If the response does not address the QUESTION, score **0.0** and stop.
- For negative constraints, both must hold: (a) the response is responsive to the QUESTION, and (b) the prohibited element is absent.

## SEMANTIC TOLERANCE RULES:
Judge by meaning, not exact wording.
- Accept **paraphrases** and **synonyms** that preserve intent.
- **Case/punctuation/whitespace** differences must be ignored.
- **Numbers/currencies/dates** may appear in equivalent forms (e.g., “$68,000”, “68k”, “68,000 USD”, or “sixty-eight thousand dollars”). Treat them as equal when numerically equivalent.
- If the rubric expects a number or duration, prefer **normalized comparison** (extract and compare values) over string matching.

## STYLE NEUTRALITY (prevents style contamination):
Ignore tone, politeness, length, and flourish unless the rubric explicitly requires a format/structure (e.g., “itemized list”, “no citations”, “one sentence”).
- Do **not** penalize hedging, voice, or verbosity if content satisfies the rubric.
- Only evaluate format when the rubric **explicitly** mandates it.

## SCORING SCALE:
- **1.0 (Complete Compliance)**: Fully complies with the rubric criterion.
  - Positive: required element present, accurate, properly executed (allowing semantic equivalents).
  - Negative: prohibited element **absent** AND response is **responsive**.

- **0.5 (Partial Compliance)**: Partially complies.
  - Positive: element present but minor inaccuracies/incomplete execution.
  - Negative: generally responsive and mostly avoids the prohibited element but with minor/edge violations.

- **0.0 (No Compliance)**: Fails to comply.
  - Positive: required element missing or incorrect.
  - Negative: prohibited element present **or** response is non-responsive/evasive even if the element is absent.

## EVALUATION INSTRUCTIONS:
1. **Understand the Requirement**: Determine if the rubric is asking for something to be present (positive) or absent (negative/constraint).

2. **Parse Compound Statements**: If the rubric contains multiple elements connected by "and" or commas, evaluate whether:
   - **All elements** must be present for full compliance (1.0)
   - **Some elements** present indicates partial compliance (0.5)
   - **No elements** present indicates no compliance (0.0)

3. **Check Compliance**:
   - For positive requirements: Look for the presence and quality of the required element
   - For negative constraints: Look for the absence of the prohibited element

4. **Assign Score**: Based on compliance with the specific rubric criterion according to the scoring scale above.

5. **Provide Reasoning**: Explain whether the rubric criterion was satisfied and justify the score.

## OUTPUT FORMAT:
Return your evaluation in JSON format with two fields:

{
   "score": [your score: 1.0, 0.5, or 0.0],
   "reason": "[detailed explanation of whether the rubric criterion was satisfied and why this justified the assigned score]"
}

NOTE: ONLY output the json object, without any explanation before or after that
"""


def format_prompt(rubric_item: str, response: str, question: str = "") -> str:
    """Format unified_llm_judge_base_prompt by substituting <rubric_item> and <llm_response>."""
    prompt = UNIFIED_LLM_JUDGE_BASE_PROMPT.replace("<rubric_item>", rubric_item).replace(
        "<llm_response>", response.strip()
    )
    if "<question>" in prompt and question:
        prompt = prompt.replace("<question>", question.strip())
    return prompt


def parse_json_response(raw: str) -> dict[str, Any]:
    """Parse JSON object from response, handling markdown fences and surrounding text."""
    text = raw.strip()
    if text.startswith("```"):
        match = re.search(r"```(?:json)?\s*(\[.*\]|\{.*\})\s*```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    match = re.search(r"(\{.*?\}|\[.*?\])", text, re.DOTALL)
    if match:
        json_part = match.group(1)
        try:
            parsed = json.loads(json_part)
            if isinstance(parsed, dict):
                return parsed
        except Exception as e:
            raise ValueError(f"Found possible JSON but failed to parse it: {e}") from e

    raise ValueError(f"No valid JSON found in response: {raw!r}")


def parse_verdict(raw: str) -> dict[str, Any]:
    """Parse score and reason from LLM judge response, mapping to 3-level rubric (1.0, 0.5, 0.0)."""
    if not raw or not raw.strip():
        return {"score": 0.0, "reason": "Empty judge response", "raw": raw}

    try:
        data = parse_json_response(raw)
        raw_score = float(data.get("score", 0.0))
        reason = str(data.get("reason", "")).strip()
    except Exception:
        # Fallback regex extraction
        match = re.search(r'"score"\s*:\s*([\d.]+)', raw)
        raw_score = float(match.group(1)) if match else 0.0
        reason_match = re.search(r'"reason"\s*:\s*"([^"]*)"', raw)
        reason = reason_match.group(1) if reason_match else ""

    # Map to official 3-level rubric scoring
    if raw_score >= 0.75:
        score_val = 1.0
    elif raw_score >= 0.25:
        score_val = 0.5
    else:
        score_val = 0.0

    return {"score": score_val, "reason": reason, "raw": raw}


def parse_response(raw: str) -> dict[str, Any]:
    """Alias for parse_verdict returning rubric score and reason."""
    return parse_verdict(raw)


def score(raw: str) -> float:
    """Return numeric 3-level rubric score (1.0, 0.5, 0.0)."""
    return float(parse_verdict(raw)["score"])


def kendall_tau_b(x: list[int | float], y: list[int | float]) -> float:
    """Compute Kendall's tau-b rank correlation coefficient in pure Python.

    Handles ties in x and y. Returns value in [-1.0, 1.0], or 0.0 if computation
    is degenerate (length < 2 or all tied).
    """
    n = len(x)
    if n < 2 or len(y) != n:
        return 0.0

    concordant = 0
    discordant = 0
    tied_x = 0
    tied_y = 0

    for i, j in combinations(range(n), 2):
        dx = x[i] - x[j]
        dy = y[i] - y[j]

        if dx == 0 and dy == 0:
            tied_x += 1
            tied_y += 1
        elif dx == 0:
            tied_x += 1
        elif dy == 0:
            tied_y += 1
        elif (dx > 0 and dy > 0) or (dx < 0 and dy < 0):
            concordant += 1
        else:
            discordant += 1

    n_pairs = n * (n - 1) // 2
    denom_x = n_pairs - tied_x
    denom_y = n_pairs - tied_y

    if denom_x <= 0 or denom_y <= 0:
        return 0.0

    return (concordant - discordant) / ((denom_x * denom_y) ** 0.5)


def _norm(s: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", s.lower()))


def event_ordering_score(
    reference_list: list[str],
    system_list: list[str],
    alignment: dict[int, int] | None = None,
) -> dict[str, Any]:
    """Computes BEAM event_ordering metric combining pure-python Kendall's tau-b and F1.

    Final score = tau_b_normalized * event_f1.
    """
    if not reference_list:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0, "tau_b": 0.0, "tau_norm": 0.0, "final_score": 0.0, "score": 0.0}

    # Derive system_canon by aligning system items to reference items
    if alignment is not None:
        system_canon = []
        for i, s in enumerate(system_list):
            ref_idx = alignment.get(i, -1)
            if 0 <= ref_idx < len(reference_list):
                system_canon.append(reference_list[ref_idx])
            else:
                system_canon.append(s)
    else:
        # Heuristic alignment by normalized string equality
        used_ref = set()
        system_canon = []
        for s in system_list:
            s_clean = _norm(s)
            matched = None
            for idx, r in enumerate(reference_list):
                if idx in used_ref:
                    continue
                r_clean = _norm(r)
                if s_clean and r_clean and (s_clean == r_clean or s_clean in r_clean or r_clean in s_clean):
                    matched = idx
                    break
            if matched is not None:
                system_canon.append(reference_list[matched])
                used_ref.add(matched)
            else:
                system_canon.append(s)

    reference_canon = list(reference_list)

    tp = len(set(reference_canon) & set(system_canon))
    fp = len([x for x in system_canon if x not in reference_canon])
    fn = len([x for x in reference_canon if x not in system_canon])

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    union = list(dict.fromkeys(reference_canon + system_canon))
    tie_rank = len(union) + 1

    r_rank = {item: i + 1 for i, item in enumerate(reference_canon)}
    s_rank = {item: i + 1 for i, item in enumerate(system_canon)}

    ref_ranks = [r_rank.get(u, tie_rank) for u in union]
    sys_ranks = [s_rank.get(u, tie_rank) for u in union]

    tau_b = kendall_tau_b(ref_ranks, sys_ranks)
    tau_b_norm = (tau_b + 1) / 2 if tau_b is not None else 0.0
    final_score = tau_b_norm * f1

    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "tau_b": tau_b,
        "tau_norm": tau_b_norm,
        "final_score": final_score,
        "score": final_score,
    }


def evaluate_rubric(
    rubric: list[str] | str,
    response: str,
    *,
    question: str = "",
    complete_fn: Callable[[str], tuple[str, dict]] | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Evaluate candidate response against each rubric criterion using unified_llm_judge_base_prompt."""
    if isinstance(rubric, str):
        # Handle string or JSON encoded rubric
        try:
            loaded = json.loads(rubric)
            items = loaded if isinstance(loaded, list) else [rubric]
        except Exception:
            items = [rubric]
    else:
        items = list(rubric)

    if not items:
        return {"score": 0.0, "rubric_scores": [], "responses": []}

    scores: list[float] = []
    responses: list[dict[str, Any]] = []

    for item in items:
        prompt = format_prompt(rubric_item=str(item), response=response, question=question)
        if complete_fn is not None:
            raw, _ = complete_fn(prompt)
        else:
            from commontrace import llm

            cfg = llm.Config(
                provider="openai-compatible",
                model=model or DEFAULT_MODEL,
                api_key=llm.load_config().api_key if hasattr(llm, "load_config") else "",
            ) if model else None
            raw, _ = llm.complete(prompt, config=cfg)

        verdict = parse_verdict(raw)
        scores.append(verdict["score"])
        responses.append({"criterion": item, "score": verdict["score"], "reason": verdict["reason"]})

    mean_score = sum(scores) / len(scores) if scores else 0.0
    return {
        "score": mean_score,
        "rubric_scores": scores,
        "responses": responses,
    }


def evaluate_abstention(
    rubric: list[str] | str,
    response: str,
    question: str = "",
    complete_fn: Callable[[str], tuple[str, dict]] | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Evaluate abstention question: verifies appropriate refusal or abstention criteria."""
    items = [rubric] if isinstance(rubric, str) else list(rubric)
    if not items:
        # Check standard refusal signals
        refusal_patterns = [
            "do not have enough information",
            "not mentioned",
            "cannot answer",
            "not provided",
            "unknown",
            "no information",
            "do not know",
            "don't know",
            "don't have",
            "do not have",
            "cannot determine",
            "not found",
        ]
        resp_lower = response.lower()
        abstained = any(p in resp_lower for p in refusal_patterns)
        return {"score": 1.0 if abstained else 0.0, "responses": []}

    return evaluate_rubric(items, response, question=question, complete_fn=complete_fn, model=model)


def evaluate_ability(
    ability: str,
    rubric: list[str] | str,
    response: str,
    question: str = "",
    complete_fn: Callable[[str], tuple[str, dict]] | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Evaluate a BEAM question according to its specific ability type."""
    ability_clean = ability.strip().lower().replace("-", "_")

    if ability_clean == "event_ordering":
        items = [rubric] if isinstance(rubric, str) else list(rubric)
        sys_items = [line.strip() for line in response.split("\n") if line.strip()]
        return event_ordering_score(reference_list=items, system_list=sys_items)
    elif ability_clean == "abstention":
        return evaluate_abstention(rubric=rubric, response=response, question=question, complete_fn=complete_fn, model=model)
    else:
        return evaluate_rubric(rubric=rubric, response=response, question=question, complete_fn=complete_fn, model=model)


class BEAMJudge:
    """Official BEAM benchmark judge implementing unified_llm_judge_base_prompt."""

    name: str = "beam"
    default_model: str = DEFAULT_MODEL
    prompt_template: str = UNIFIED_LLM_JUDGE_BASE_PROMPT

    def __init__(self, model: str | None = None):
        self.model = model or self.default_model

    def format_prompt(self, rubric_item: str, response: str, question: str = "") -> str:
        return format_prompt(rubric_item=rubric_item, response=response, question=question)

    def parse_response(self, response: str) -> dict[str, Any]:
        return parse_response(response)

    def score(self, response: str) -> float:
        return score(response)

    def evaluate_event_ordering(
        self,
        reference_list: list[str],
        system_list: list[str],
        alignment: dict[int, int] | None = None,
    ) -> dict[str, Any]:
        return event_ordering_score(reference_list, system_list, alignment=alignment)

    def evaluate_rubric(
        self,
        rubric: list[str] | str,
        response: str,
        question: str = "",
        complete_fn: Callable[[str], tuple[str, dict]] | None = None,
    ) -> dict[str, Any]:
        return evaluate_rubric(
            rubric=rubric,
            response=response,
            question=question,
            complete_fn=complete_fn,
            model=self.model,
        )

    def evaluate_ability(
        self,
        ability: str,
        rubric: list[str] | str,
        response: str,
        question: str = "",
        complete_fn: Callable[[str], tuple[str, dict]] | None = None,
    ) -> dict[str, Any]:
        return evaluate_ability(
            ability=ability,
            rubric=rubric,
            response=response,
            question=question,
            complete_fn=complete_fn,
            model=self.model,
        )

    def grade(
        self,
        question: dict[str, Any] | str,
        answer: str,
        *,
        ability: str | None = None,
        rubric: list[str] | str | None = None,
        complete_fn: Callable[[str], tuple[str, dict]] | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Grade a candidate answer against BEAM rubric."""
        if isinstance(question, dict):
            q_text = question.get("question", "")
            q_type = ability or question.get("type", "information_extraction")
            rubric_data = rubric or question.get("rubric", question.get("answer", ""))
        else:
            q_text = str(question)
            q_type = ability or kwargs.get("type", "information_extraction")
            rubric_data = rubric if rubric is not None else kwargs.get("rubric", kwargs.get("answer", ""))

        res = self.evaluate_ability(
            ability=str(q_type),
            rubric=rubric_data,
            response=answer,
            question=q_text,
            complete_fn=complete_fn,
        )

        final_sc = float(res.get("score", res.get("final_score", 0.0)))
        return {
            "answer": answer.strip(),
            "correct": final_sc >= 0.5,
            "score": final_sc,
            "ability": q_type,
            "details": res,
            "model": model or self.model,
        }
