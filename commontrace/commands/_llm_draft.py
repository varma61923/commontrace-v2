from __future__ import annotations

import sys

from commontrace import llm

MAX_EVIDENCE_CHARS = 16_000


def _within_budget(lines: list[str], limit: int = MAX_EVIDENCE_CHARS) -> list[str]:
    kept, used = [], 0
    for i, line in enumerate(lines):
        used += len(line) + 1
        if used > limit:
            return kept + [f"({len(lines) - i} further evidence line(s) omitted to bound the prompt.)"]
        kept.append(line)
    return kept


def prompt(instruction: str, slug: str, current_rule_text: str, applies_when: str,
           do_not_apply_when: str, evidence_lines: list[str]) -> str:
    return (
        f"{instruction}\n\n"
        f"Lesson slug: {slug}\n"
        f"Current rule text:\n{current_rule_text}\n\n"
        f"Current applies_when: {applies_when}\n"
        f"Current do_not_apply_when: {do_not_apply_when}\n\n"
        "Evidence (the ONLY occasions/traces you may cite -- do not invent others):\n"
        + "\n".join(_within_budget(evidence_lines)) +
        "\n\nRespond with ONLY a JSON object, no other text, with exactly these keys:\n"
        '  "rule": string,\n'
        '  "applies_when": string,\n'
        '  "do_not_apply_when": string,\n'
        '  "evidence": array of occasion/trace ids from the list above that support your answer.\n'
    )


def try_draft(
    *, instruction: str, slug: str, current_rule_text: str, applies_when: str,
    do_not_apply_when: str, evidence_lines: list[str], allowed_evidence_ids: set[str],
) -> llm.Draft | None:
    built = prompt(instruction, slug, current_rule_text, applies_when, do_not_apply_when, evidence_lines)
    try:
        return llm.draft(built, allowed_evidence_ids=allowed_evidence_ids)
    except (llm.LLMUnavailable, llm.LLMDraftRejected) as exc:
        print(
            f"[commontrace] --draft requested but no usable LLM draft was produced "
            f"({type(exc).__name__}: {exc}) -- falling back to the template scaffold.",
            file=sys.stderr,
        )
        return None
