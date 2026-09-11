"""Turns that cannot be worth a retrieval -- and the ones that only look like it.

Most of this file is negative cases. A gate that skips retrieval is only safe
if it never swallows a real query, and the dangerous inputs are not the
acknowledgements it is built for -- they are the real questions that happen to
START with one. "no results come back when the token expires" shares its first
word with "no".
"""
from __future__ import annotations

import pytest

from commontrace.cache_gate import is_trivial_prompt

TRIVIAL = [
    "ok", "OK!", "okay.", "k", "kk",
    "yes", "no", "y", "n", "yep", "nope", "nah", "yeah",
    "thanks", "Thanks!", "thank you", "ty", "cheers",
    "hi", "hey", "hello", "yo", "good morning",
    "continue", "go ahead", "proceed.", "do it", "carry on",
    "got it", "understood", "makes sense", "sounds good", "agreed",
    "cool", "nice", "great", "perfect", "done", "next", "lgtm", "ship it",
    "please", "now", "again",
    "cool!!!", "  lgtm  ", "sure...", "ok?",
]

# Real queries. Every one of these must reach the ranker.
REAL = [
    "no results come back when the token expires",
    "continue the deployment after the migration finishes",
    "ok so the connection pool is exhausted under load",
    "done deal but the webhook never fires",
    "yes but why does the retry loop spin forever",
    "good morning the primary database is down",
    "thanks for nothing, the search index is corrupt",
    "next.js build fails on the CI runner",
    "nice to have: idempotent writes on the outbox",
    "restart",
    "rerun the failing job",
    "k8s pod crashloops after the config change",
    "proceed with caution: the migration is not reversible",
    "hello world service returns 500 on startup",
    "great expectations validation fails on null columns",
]


class TestItCatchesContentlessTurns:
    @pytest.mark.parametrize("text", TRIVIAL)
    def test_an_acknowledgement_is_trivial(self, text):
        assert is_trivial_prompt(text) is True

    def test_nothing_at_all_is_trivial(self):
        assert is_trivial_prompt("") is True
        assert is_trivial_prompt("   \n\t ") is True
        assert is_trivial_prompt(None) is True


class TestItNeverSwallowsARealQuery:
    """The property that decides whether this gate is safe to ship."""

    @pytest.mark.parametrize("text", REAL)
    def test_a_real_query_reaches_the_ranker(self, text):
        assert is_trivial_prompt(text) is False, (
            f"{text!r} would have been silently skipped"
        )

    def test_sharing_a_first_word_is_not_enough(self):
        """The whole turn has to be an acknowledgement, not merely start
        with one -- which is why the pattern is anchored at both ends."""
        assert is_trivial_prompt("no") is True
        assert is_trivial_prompt("no retries are configured") is False

    def test_short_and_low_entropy_is_not_the_test(self):
        """`restart` and `rerun` are as short and as low-entropy as `ok`, and
        both are real questions about real failure modes. The list is closed
        on purpose rather than being a length or entropy heuristic."""
        assert is_trivial_prompt("restart") is False
        assert is_trivial_prompt("rerun") is False
        assert is_trivial_prompt("rollback") is False
        assert is_trivial_prompt("timeout") is False
