"""The reader prompt the product and the benchmarks share: shape routing and notes."""
from __future__ import annotations

import pytest

from unimem.conversation.answer import ABSTAIN_NOTE, GUIDANCE, answer_prompt, question_shapes


@pytest.mark.parametrize("question, shapes", [
    ("How many times did I go hiking in total?", ["counting"]),
    ("When did Caroline go to the LGBTQ support group?", ["time"]),
    ("How many days passed between the concert and the trip?", ["time"]),
    ("In what order did I bring up the budget tracker features?", ["ordering"]),
    ("Can you summarize my progress on the app?", ["summary"]),
    ("Can you recommend a good hotel for my trip?", ["suggestion"]),
    ("Could you show me how to implement a login feature?", ["task"]),
    ("How do I implement JWT authentication?", ["task"]),
    ("What is Melanie's favorite color?", []),
    ("Would Caroline likely pursue writing as a career?", []),
])
def test_question_shapes_route_guidance_from_the_question_alone(question, shapes):
    assert question_shapes(question) == shapes


def test_prompt_carries_only_the_matching_guidance_and_the_inputs():
    prompt = answer_prompt("[session 1 · Monday 1 May 2023]\nAna: I adopted a cat.", "How many cats do I have?",
                           "2 May 2023")
    assert GUIDANCE["counting"] in prompt
    assert all(GUIDANCE[other] not in prompt for other in GUIDANCE if other != "counting")
    assert "Ana: I adopted a cat." in prompt
    assert prompt.rstrip().endswith("Question (asked 2 May 2023): How many cats do I have?\nAnswer:")


def test_abstain_note_and_empty_context():
    assert ABSTAIN_NOTE in answer_prompt("Ana: hello", "What is Ana's car?", "now", abstain=True)
    assert ABSTAIN_NOTE not in answer_prompt("Ana: hello", "What is Ana's car?", "now")
    assert "(no memories)" in answer_prompt("", "What is Ana's car?", "now")


def test_braces_in_memories_do_not_break_formatting():
    prompt = answer_prompt('config = {"a": 1} and {placeholder}', "What did I configure?", "now")
    assert '{"a": 1} and {placeholder}' in prompt
