"""Ranking rules that once let a boost outvote the one turn that answers the question."""
from __future__ import annotations

from commontrace.conversation import Options, Store, recall, search

LEX = {"embedder": None, "rerank": None, "summaries": False, "instructions": 0, "profile_facts": 0}


def test_current_questions_reorder_only_comparable_matches(tmp_path):
    # "currently" asks for the latest statement among near-equal matches; it must not
    # lift later chatter that merely shares the word over the turn about shampoo
    with Store(str(tmp_path), "s") as store:
        store.add("old", [{"role": "user", "text": "I use a lavender shampoo from Trader Joe's."}],
                  session_at="2023-05-01")
        for i in range(12):
            store.add(f"chat{i}", [{"role": "user", "text": f"I currently use headphones model {i} at work."}],
                      session_at=f"2023-05-{i + 10:02d}")
        result = recall(store, "What brand of shampoo do I currently use?", options=Options(budget=400, **LEX))
        assert "lavender shampoo" in result.context
        # only the top `recency_pool` matches compete on time; before, it fell below all twelve
        assert result.ranked.index(1) < Options().recency_pool


def test_date_gap_questions_search_each_event():
    assert search.gap_events("How many days passed between when I obtained my API key and when I "
                             "completed the UI wireframe?") == ["I obtained my API key", "I completed the UI wireframe"]
    assert search.gap_events("How many days after I submitted my cover letter did I have my follow-up "
                             "with Greg?") == ["I submitted my cover letter", "have my follow-up with Greg"]
    assert search.gap_events("How many days had I been tracking my expenses before I felt frustrated "
                             "enough to stop?") == ["tracking my expenses", "felt frustrated enough to stop"]
    assert search.gap_events("What is my favourite colour?") == []
    subs = search.subqueries("How many days had passed between the day I bought a gift for my brother "
                             "and the day I bought a birthday gift for my best friend?")
    assert "I bought a gift for my brother" in subs and "I bought a birthday gift for my best friend" in subs


def test_breadth_is_for_counts_and_aggregates_not_single_answers():
    assert search.is_broad("How many times did I go hiking this year?")
    assert search.is_broad("How much money did I raise in total through all the charity events?")
    assert search.is_broad("How many days passed between when I got the key and when I finished the wireframe?")
    # one earlier answer, even when it is a count
    assert not search.is_broad("I was looking back at our previous chat: how many times did the Chiefs play "
                               "the Jaguars at Arrowhead?")
    assert not search.is_broad("Can you remind me what you said about the Lost Temple one-shot?")
    # a duration of a habit is not a whole-history question
    assert not search.is_broad("How many hours do I sleep on weekdays?")
    assert search.is_broad("Can you remind me of the order in which we covered the topics?")


def test_when_boost_is_for_dates_not_durations():
    assert search._ASKS_WHEN.search("When did I start yoga?")
    assert search._ASKS_WHEN.search("How many days passed between the two trips?")
    assert search._ASKS_WHEN.search("How long ago did I move?")
    assert not search._ASKS_WHEN.search("How much time do I dedicate to practicing guitar every day?")
    assert not search._ASKS_WHEN.search("How long is my commute?")


def test_choice_questions_split_but_compound_subjects_do_not():
    assert len(search.subqueries("Did I prefer tea or coffee?")) > 1
    subs = search.subqueries("What is the total cost of Lola's vet visit and flea medication?")
    assert not any(s.endswith("flea medication?") and "visit" not in s for s in subs)


def test_recency_pool_is_part_of_the_recall_cache_key(tmp_path):
    with Store(str(tmp_path), "s") as store:
        store.add("a", [{"role": "user", "text": "My bike is red."}], session_at="2024-01-01")
        store.add("b", [{"role": "user", "text": "My bike is blue now."}], session_at="2024-02-01")
        narrow = recall(store, "What colour is my bike now?", options=Options(budget=200, recency_pool=1, **LEX))
        wide = recall(store, "What colour is my bike now?", options=Options(budget=200, recency_pool=10, **LEX))
        assert wide.ranked[0] == 2
        assert narrow is not wide


def test_filtered_keyword_search_matches_the_exact_sql_filter(tmp_path):
    import json
    import random

    rng = random.Random(7)
    words = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima".split()
    with Store(str(tmp_path), "s") as store:
        for day in range(1, 9):
            store.add(f"s{day}", [{"role": "user", "text": " ".join(rng.choice(words) for _ in range(12))}
                                  for _ in range(15)], session_at=f"2024-01-{day:02d}")
        turns = [r[0] for r in store.db.execute("SELECT id FROM turns")]
        for size in (3, 30, 110):
            allowed = set(rng.sample(turns, size))
            for query in ("alpha bravo", "kilo", "echo lima golf"):
                fast = store.lexical(query, 10, allowed=allowed)
                exact = list(store.db.execute(
                    "SELECT rowid FROM units_fts WHERE units_fts MATCH ? AND rowid IN (SELECT id FROM units "
                    "WHERE turn IN (SELECT value FROM json_each(?))) ORDER BY bm25(units_fts), rowid LIMIT 10",
                    (" OR ".join(f'"{w}"' for w in query.split()), json.dumps(sorted(allowed)))))
                assert [u for u, _s in fast] == [r[0] for r in exact]


def test_a_filter_that_excludes_nothing_is_dropped_from_search(tmp_path, monkeypatch):
    with Store(str(tmp_path), "s") as store:
        store.add("a", [{"role": "user", "text": "hello there"}], session_at="2024-01-01")
        assert store.allowed(until="2030-01-01") == {1}  # the store API still reports the set
        seen = []
        real = store.lexical
        monkeypatch.setattr(store, "lexical", lambda q, n, allowed=None: seen.append(allowed) or real(q, n, allowed=allowed))
        recall(store, "hello", now="2030-01-01", options=Options(budget=100, **LEX))
        assert seen and all(a is None for a in seen)


def test_a_users_own_plans_are_not_standing_instructions():
    from commontrace.conversation import profile

    def kinds(text):
        return [f.kind for f in profile.extract(text)]

    assert "instruction" not in kinds("I'll make sure to prune my basil plant regularly.")
    assert "instruction" not in kinds("I need to remember to call my mom.")
    assert "instruction" not in kinds("I'm planting succulents, and I'll make sure to leave space between them.")
    assert kinds("Make sure to always cite sources in your answers.") == ["instruction"]
    assert kinds("Remember to keep answers under 100 words.") == ["instruction"]
    assert kinds("From now on, use metric units.") == ["instruction"]


def test_frequency_lookup_matches_full_decoding():
    from collections import Counter

    from commontrace.fact_index import _frequencies

    for counts in (Counter({"alpha": 1, "beta": 3, "gamma": 2}), Counter({"alpha": 1, "beta": 300, "gamma": 2})):
        packed = _frequencies(counts)
        assert {term: packed.get(term) for term in counts} == dict(packed) == dict(counts)
        assert packed.get("delta") == 0 and packed.get("") == 0


def test_ordering_questions_list_every_aspect_the_user_raised(tmp_path):
    aspects = ["setting up the repository", "choosing a charting library", "writing unit tests for parsing",
               "adding dark mode", "deploying to a cheap host", "fixing a memory leak in the worker",
               "localising dates for Japan", "tuning the database indexes"]
    with Store(str(tmp_path), "s") as store:
        for day, aspect in enumerate(aspects, 1):
            store.add(f"s{day}", [{"role": "user", "text": f"Today I want help with {aspect} in my budget app."},
                                  {"role": "assistant", "text": "Sure, here is a detailed walkthrough. " * 30}],
                      session_at=f"2024-03-{day:02d}")
        question = "Can you list the order in which I brought up different aspects of my budget app?"
        result = recall(store, question, options=Options(budget=600, **LEX))
        positions = [result.context.find(aspect) for aspect in aspects]
        assert all(p >= 0 for p in positions) and positions == sorted(positions)
        assert "walkthrough" not in result.context  # the user's own turns only
        plain = recall(store, "What did I want help with on the charting library?", options=Options(budget=600, **LEX))
        assert "walkthrough" in plain.context  # other questions still read the assistant's replies


def test_summary_questions_read_each_ask_with_its_answer(tmp_path):
    steps = ["schema design", "login flow", "payment webhooks", "rate limiting", "audit logging",
             "backup policy", "load testing", "launch checklist"]
    with Store(str(tmp_path), "s") as store:
        for day, step in enumerate(steps, 1):
            store.add(f"s{day}", [{"role": "user", "text": f"For my shop project, how should I handle {step}?"},
                                  {"role": "assistant", "text": f"For {step}, start with answer-{day}. " + "Detail. " * 80}],
                      session_at=f"2024-04-{day:02d}")
        result = recall(store, "Can you give me a summary of how my shop project progressed?",
                        options=Options(budget=1500, **LEX))
        answered = [day for day in range(1, 9) if f"answer-{day}" in result.context]
        assert len(answered) >= 7  # nearly every exchange, ask and answer, within the budget


def test_considering_questions_search_each_named_aspect():
    subs = search.subqueries("Considering my form validation code, lazy loading setup, GA4 anonymized tracking, "
                             "and bounce rate monitoring, how can I improve conversions?")
    assert {"form validation code", "lazy loading setup", "GA4 anonymized tracking", "bounce rate monitoring"} <= set(subs)


def test_counting_what_the_user_mentioned_lists_their_turns(tmp_path):
    sizes = ["size 9 trail runners", "size 9.5 sandals", "size 10 boots", "size 8 slippers"]
    with Store(str(tmp_path), "s") as store:
        for day, size in enumerate(sizes, 1):
            store.add(f"s{day}", [{"role": "user", "text": f"I just ordered {size} for the trip."},
                                  {"role": "assistant", "text": "Great choice for walking. " * 40}],
                      session_at=f"2024-05-{day:02d}")
        result = recall(store, "How many different shoe sizes did I mention across my conversations?",
                        options=Options(budget=400, **LEX))
        assert all(size in result.context for size in sizes)
