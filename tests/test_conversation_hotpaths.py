"""Exact-output and work-count checks for linear conversation preprocessing."""
import datetime as dt
import random
import re

import pytest

from commontrace.conversation import timeparse
from commontrace.conversation.store import split_units


def _previous_units(text, limit):
    """Frozen pre-optimization behavior; used on small randomized inputs only."""
    text = text.strip()
    if len(text) <= limit:
        return [text]
    units, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+|\n{2,}|\n(?=[-*\d])", text):
        sentence = sentence.strip()
        if not sentence:
            continue
        while len(sentence) > limit:
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            if current:
                units.append(current)
                current = ""
            units.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if current and len(current) + 1 + len(sentence) > limit:
            units.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        units.append(current)
    return units


def test_chunk_boundaries_match_previous_behavior_for_unicode_and_punctuation():
    rng = random.Random(61923)
    alphabet = "abcd .!?\n\t\u2003\u00a0-*12λ"
    for _ in range(500):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randrange(400)))
        limit = rng.randrange(1, 80)
        assert split_units(text, limit) == _previous_units(text, limit)


@pytest.mark.parametrize("limit", [0, -1, -700])
def test_invalid_unit_size_cannot_hang(limit):
    with pytest.raises(ValueError, match="positive"):
        split_units("a long interaction", limit)


def test_long_unbroken_turn_retains_every_character():
    text = "λ" * 100_003
    units = split_units(text)
    assert "".join(units) == text
    assert all(0 < len(unit) <= 700 for unit in units)


def test_repeated_weekdays_evaluate_each_clause_once(monkeypatch):
    counts = {"future": 0, "past": 0}

    class CountedPattern:
        def __init__(self, name, pattern):
            self.name, self.pattern = name, pattern

        def search(self, text):
            counts[self.name] += 1
            return self.pattern.search(text)

    monkeypatch.setattr(timeparse, "_FUTURE", CountedPattern("future", timeparse._FUTURE))
    monkeypatch.setattr(timeparse, "_PAST", CountedPattern("past", timeparse._PAST))
    text = "We will go " + "on Tuesday and " * 1000
    groundings = timeparse.ground(text, dt.date(2026, 10, 5))
    assert len(groundings) == 1000
    assert {g.lo for g in groundings} == {dt.date(2026, 10, 6)}
    assert counts == {"future": 1, "past": 1}


def test_tense_boundaries_preserve_past_future_and_semicolon_semantics():
    text = "We will go on Tuesday. We went on Tuesday! We will go; we went on Tuesday? We will go on Tuesday"
    groundings = timeparse.ground(text, dt.date(2026, 10, 5))
    assert [g.lo for g in groundings] == [dt.date(2026, 10, 6), dt.date(2026, 9, 29),
                                        dt.date(2026, 9, 29), dt.date(2026, 10, 6)]


def test_clause_index_matches_frozen_boundary_rules():
    text = "Will go on Tuesday? Went on Monday. We will go! on Friday\nwas busy; on Saturday"
    indexed = timeparse._ClauseTense(text)
    for start in range(len(text)):
        for end in (start, min(start + 7, len(text))):
            lo = max(text.rfind(mark, 0, start) for mark in ".!?") + 1
            hi = min([i for mark in ".!?" if (i := text.find(mark, end)) >= 0] or [len(text)])
            expected = bool(timeparse._FUTURE.search(text[lo:hi])) and not timeparse._PAST.search(text[lo:hi])
            assert indexed.future(start, end) == expected
