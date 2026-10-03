"""Conversation memory behaviours behind specific memory abilities: standing
instructions, preferences, long turns, broad questions, knowledge updates, abstention
signals, wall-clock dates, frozen (read-only) stores and time-gated neighbours."""
import sqlite3

import pytest

from commontrace.conversation import Options, Store, profile, recall
from commontrace.conversation.search import asks_current, is_broad

LEX = dict(embedder=None, rerank=None)


def _chat(store, session, at, pairs):
    msgs = []
    for i, (role, text) in enumerate(pairs):
        msgs.append({"id": f"{session}-{i}", "role": role, "speaker": role, "text": text})
    store.add(session, msgs, session_at=at)


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path), "beam")
    _chat(s, "s1", "2024-03-15", [
        ("user", "Always format all code snippets with syntax highlighting when I ask about implementation."),
        ("assistant", "Understood, I will use fenced code blocks."),
        ("user", "I prefer simple, minimal dependencies to keep the app lightweight."),
        ("assistant", "Noted."),
        ("user", "The dashboard API averages 400ms right now."),
        ("assistant", "Let's profile it."),
    ])
    filler = []
    for i in range(40):
        filler += [("user", f"Unrelated note number {i} about gardening tomatoes and watering schedules."),
                   ("assistant", f"Sure, gardening tip {i}: water early in the morning.")]
    _chat(s, "s2", "2024-04-01", filler)
    _chat(s, "s3", "2024-04-20", [
        ("user", "Update: after caching tweaks the dashboard API now averages around 250ms."),
        ("assistant", "Great improvement."),
    ])
    yield s
    s.close()


def test_instruction_kind_is_extracted():
    kinds = {f.kind for f in profile.extract("From now on, keep answers under 100 words.")}
    assert kinds == {"instruction"}
    assert [f.kind for f in profile.extract("Please never suggest paid tools.")] == ["instruction"]
    assert [f.kind for f in profile.extract("I'd rather use SQLite than Postgres.")] == ["preference"]
    assert [f.kind for f in profile.extract("I always run tests first.")] == ["habit"]


def test_standing_instructions_are_pinned_for_unrelated_questions(store):
    r = recall(store, "Could you show me how to implement a login feature?", options=Options(budget=600, **LEX))
    assert "[Standing instructions from the user]" in r.context
    assert "syntax highlighting" in r.context
    source = [t for t in store.turns(r.turns).values() if t.ref == "s1-0"]
    assert source, "the instruction's own turn counts as recalled"


def test_instructions_are_not_pinned_between_two_people(tmp_path):
    with Store(str(tmp_path), "friends") as s:
        s.add("s1", [{"speaker": "Ana", "text": "Always make time for family, that's my rule."},
                     {"speaker": "Ben", "text": "Agreed, and we should plan a trip."}], session_at="2023-01-01")
        r = recall(s, "what about a trip?", options=Options(budget=400, **LEX))
        assert "[Standing instructions" not in r.context


def test_preferences_shape_requests_for_help(store):
    r = recall(store, "What libraries would you suggest for login and analytics?", options=Options(budget=600, **LEX))
    assert "minimal dependencies" in r.context


def test_knowledge_update_prefers_the_later_statement(store):
    assert asks_current("What is the dashboard API response time now?")
    r = recall(store, "What is the dashboard API response time now?", options=Options(budget=200, **LEX))
    assert "250ms" in r.context
    assert r.explain.get("recency") is True


def test_long_turns_are_excerpted_not_dropped(tmp_path):
    with Store(str(tmp_path), "s") as s:
        pasted = " ".join(f"Line {i} of the build log is unremarkable." for i in range(600))
        pasted += " The failing step was the alembic migration 0042 on the orders table. "
        pasted += " ".join(f"Trailing line {i} is unremarkable." for i in range(600))
        s.add("x", [{"role": "user", "text": pasted}, {"role": "assistant", "text": "Look at the migration."}],
              session_at="2024-01-01")
        r = recall(s, "which migration failed on the orders table?", options=Options(budget=500, **LEX))
        assert "alembic migration 0042" in r.context
        assert r.tokens <= 520 and "…" in r.context


def test_broad_questions_cover_what_the_user_raised(store):
    assert is_broad("Can you summarise how my project progressed?")
    assert is_broad("In what order did I bring up these topics?")
    assert not is_broad("What is my dashboard API latency?")
    r = recall(store, "Give me a summary of everything about the dashboard API throughout our conversations",
               options=Options(budget=500, **LEX))
    refs = {t.ref for t in store.turns(r.turns).values()}
    assert {"s1-4", "s3-0"} <= refs


def test_confidence_is_reported(store):
    r = recall(store, "What does the dashboard API average?", options=Options(budget=300, **LEX))
    assert 0.0 <= r.explain["confidence"] <= 1.0
    assert r.explain["confidence"] > 0.5


def test_wall_clock_dates_are_kept(tmp_path):
    with Store(str(tmp_path), "s") as s:
        s.add("d", [{"role": "user", "text": "Dinner with Sam tonight.", "at": "2023-05-08T19:30:00-08:00"}])
        turn = next(iter(s.turns([1]).values()))
        assert turn.at.day == 8 and turn.at.hour == 19


def test_frozen_store_recalls_but_refuses_writes(tmp_path):
    with Store(str(tmp_path), "s") as s:
        s.add("d", [{"role": "user", "text": "I keep my passport in the blue drawer."}], session_at="2023-01-01")
    frozen = Store(str(tmp_path), "s", read_only=True)
    try:
        r = recall(frozen, "where is my passport?", options=Options(budget=200, **LEX))
        assert "blue drawer" in r.context
        with pytest.raises(sqlite3.OperationalError):
            frozen.add("d", [{"role": "user", "text": "new"}])
    finally:
        frozen.close()
    with pytest.raises(Exception):
        Store(str(tmp_path), "missing", read_only=True)


def test_neighbours_are_time_gated(tmp_path):
    with Store(str(tmp_path), "notes") as s:
        s.add("2024-01-01", [
            {"id": "a", "role": "user", "text": "Morning: dentist moved to Friday.", "at": "2024-01-01T08:00"},
            {"id": "b", "role": "user", "text": "Evening: thinking about the quarterly offsite.", "at": "2024-01-01T21:00"},
        ])
        r = recall(s, "when is the dentist?", options=Options(budget=400, neighbours_before=2, neighbours_after=2,
                                                            profile_facts=0, **LEX))
        assert "dentist" in r.context and "offsite" not in r.context


def test_list_questions_search_each_aspect():
    from commontrace.conversation.search import subqueries

    subs = subqueries("Can you give a comprehensive summary of my budget tracker project, including the key "
                      "features implemented, security enhancements, and documentation efforts?")
    assert "budget tracker project security enhancements" in subs
    assert "budget tracker project documentation efforts" in subs
    assert subqueries("Where did I go last week?") == ["Where did I go last week?"]


def test_self_introductions_are_identity_and_answer_profession_questions(tmp_path):
    facts = profile.extract("I'm Craig, a 44-year-old colour technologist, and I'm learning probability.")
    assert [(f.kind, f.subject) for f in facts] == [("identity", "44-year-old colour technologist")]
    with Store(str(tmp_path), "s") as s:
        s.add("a", [{"role": "user", "text": "I'm Craig, a colour technologist, and I want help with charts."},
                    {"role": "assistant", "text": "Happy to help with charts."}], session_at="2024-01-01")
        r = recall(s, "What profession did I mention?", options=Options(budget=300, **LEX))
        assert "colour technologist" in r.context


def test_cross_encoder_blends_with_the_fused_rank(monkeypatch):
    from commontrace import rerank_arm
    from commontrace.conversation import search

    class FakeStore:
        def turns(self, ids):
            from commontrace.conversation.store import Turn
            return {i: Turn(i, "s", i, None, "user", "user", f"turn {i}", ()) for i in ids}

    monkeypatch.setattr(rerank_arm, "ready", lambda mode: "")
    # the cross-encoder puts the fused last first; blended, the fused first keeps a high place
    monkeypatch.setattr(rerank_arm, "rerank", lambda q, ids, text, n, mode: ([(i, 10 - k) for k, i in
                                                                               enumerate(reversed(ids))], []))
    replaced = search._rerank(FakeStore(), "q", [1, 2, 3, 4], "cross-encoder", 4, None, 0.0)
    blended = search._rerank(FakeStore(), "q", [1, 2, 3, 4], "cross-encoder", 4, None, 1.0)
    assert replaced[0] == 4
    assert set(blended[:2]) == {1, 4} and blended != replaced
    assert search.Options().rerank_blend == 1.0
