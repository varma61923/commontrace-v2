"""Shared `--draft` plumbing for the commands that ask an LLM to fill in a
lesson scaffold instead of leaving a "TODO: ..." placeholder --
`lesson suggest-revision`, `lesson suggest-rewrite`, and `distill`.

One place for this rather than three, because the property that matters --
a caller offers ONLY its own already-computed evidence, and the response is
refused rather than trusted if it doesn't check out (commontrace/llm.py) --
is exactly the kind of thing that quietly drifts if each command re-derives
it. Nothing here calls a network endpoint; commontrace/llm.py does, and
this module's only job is building its prompt and turning a raised
LLMUnavailable/LLMDraftRejected into a printed reason and a `None` a caller
falls back on.
"""
from __future__ import annotations

import sys

from commontrace import llm


def prompt(instruction: str, slug: str, current_rule_text: str, applies_when: str,
           do_not_apply_when: str, evidence_lines: list[str]) -> str:
    """The prompt every `--draft` caller sends. Built here, once, so every
    caller's evidence reaches the model in the same shape -- and so the
    exact text hashed into `llm.Draft.provenance["prompt_sha256"]` is
    reconstructible by a reader of this function, not scattered per call
    site."""
    return (
        f"{instruction}\n\n"
        f"Lesson slug: {slug}\n"
        f"Current rule text:\n{current_rule_text}\n\n"
        f"Current applies_when: {applies_when}\n"
        f"Current do_not_apply_when: {do_not_apply_when}\n\n"
        "Evidence (the ONLY occasions/traces you may cite -- do not invent others):\n"
        + "\n".join(evidence_lines) +
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
    """Attempt an LLM-assisted draft; return None (after printing a stderr
    note naming why) rather than raising, so every caller degrades to its
    existing non-LLM scaffold exactly as it did before `--draft` existed."""
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
