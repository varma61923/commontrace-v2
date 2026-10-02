from commontrace import retrieval


def _lesson(name, description="", applies_when="", tags=None, domain="", importance=3, uses=0):
    return (
        f"/tmp/{name}.md",
        {
            "name": name,
            "description": description,
            "applies_when": applies_when,
            "tags": tags or [],
            "domain": domain,
            "importance": importance,
            "uses": uses,
        },
    )


def test_ranks_matching_lesson_above_unrelated_one():
    lessons = [
        _lesson("lesson_git_safety", description="Never force-push shared branches", tags=["git-safety"]),
        _lesson("lesson_unrelated", description="How to write a good changelog entry", tags=["docs"]),
    ]
    ranked = retrieval.rank_lessons("I need to force-push a shared branch", lessons)
    assert [r.slug for r in ranked] == ["lesson_git_safety"]


def test_empty_query_returns_nothing():
    lessons = [_lesson("lesson_a", description="anything")]
    assert retrieval.rank_lessons("", lessons) == []
    assert retrieval.rank_lessons("   ", lessons) == []


def test_no_match_returns_empty_list_not_error():
    lessons = [_lesson("lesson_a", description="completely unrelated content about widgets")]
    assert retrieval.rank_lessons("xylophone quokka", lessons) == []


def test_respects_top_k():
    lessons = [
        _lesson(f"lesson_{i}", description="git safety force push branch", tags=["git-safety"])
        for i in range(5)
    ]
    ranked = retrieval.rank_lessons("git safety force push branch", lessons, top_k=2)
    assert len(ranked) == 2


def test_negative_top_k_returns_nothing_not_a_python_slice_artifact():
    lessons = [
        _lesson(f"lesson_{i}", description="git safety force push branch", tags=["git-safety"])
        for i in range(5)
    ]
    ranked = retrieval.rank_lessons("git safety force push branch", lessons, top_k=-1)
    assert ranked == []


def test_tag_match_outweighs_description_only_match():
    lessons = [
        _lesson("lesson_tag_match", description="something else entirely", tags=["escalation"]),
        _lesson("lesson_prose_match", description="this is about escalation handling", tags=["other"]),
    ]
    ranked = retrieval.rank_lessons("escalation", lessons)
    assert ranked[0].slug == "lesson_tag_match"


def test_tie_breaks_toward_higher_importance():
    lessons = [
        _lesson("lesson_low_importance", description="refund policy", importance=1),
        _lesson("lesson_high_importance", description="refund policy", importance=5),
    ]
    ranked = retrieval.rank_lessons("refund policy", lessons)
    assert ranked[0].slug == "lesson_high_importance"


def test_stopwords_do_not_drive_matches():
    lessons = [_lesson("lesson_a", description="the of and to")]
    assert retrieval.rank_lessons("the of and", lessons) == []


def test_hand_edited_non_integer_importance_does_not_crash_ranking():
    lessons = [
        _lesson("lesson_a", description="refund policy", importance="high"),
        _lesson("lesson_b", description="refund policy", importance=2),
    ]
    ranked = retrieval.rank_lessons("refund policy", lessons)
    assert {r.slug for r in ranked} == {"lesson_a", "lesson_b"}


def test_lessons_with_duplicate_names_get_their_own_tie_break_fields():
    lessons = [
        _lesson("", description="refund policy", importance=1, uses=0),
        _lesson("", description="refund policy", importance=5, uses=99),
    ]
    ranked = retrieval.rank_lessons("refund policy", lessons)
    assert len(ranked) == 2
    assert ranked[0].path == lessons[1][0]
    assert ranked[1].path == lessons[0][0]


class TestReliabilityAndRecencyWeighting:
    def _tied_pair(self):
        return [
            _lesson("lesson_a", description="refund policy for enterprise accounts",
                    importance=3, uses=0),
            _lesson("lesson_b", description="refund policy for enterprise accounts",
                    importance=3, uses=0),
        ]

    def test_default_is_inert_identical_output_to_calling_with_neither_argument(self):
        lessons = self._tied_pair()
        baseline = retrieval.rank_lessons("refund policy enterprise", lessons)
        with_zero_weights = retrieval.rank_lessons(
            "refund policy enterprise", lessons,
            reliability_lookup={"lesson_a": 1.0, "lesson_b": -1.0},
            reliability_weight=0.0,
            recency_lookup={"lesson_a": 1.0, "lesson_b": -1.0},
            recency_weight=0.0,
        )
        assert [r.slug for r in baseline] == [r.slug for r in with_zero_weights]
        assert [r.relevance for r in baseline] == [r.relevance for r in with_zero_weights]

    def test_a_harmful_lesson_ranks_behind_a_reliable_one_when_otherwise_tied(self):
        lessons = self._tied_pair()
        ranked = retrieval.rank_lessons(
            "refund policy enterprise", lessons,
            reliability_lookup={"lesson_a": -1.0, "lesson_b": 1.0},
            reliability_weight=0.2,
        )
        assert [r.slug for r in ranked] == ["lesson_b", "lesson_a"]

    def test_a_stale_lesson_ranks_behind_a_fresh_one_when_otherwise_tied(self):
        lessons = self._tied_pair()
        ranked = retrieval.rank_lessons(
            "refund policy enterprise", lessons,
            recency_lookup={"lesson_a": -1.0, "lesson_b": 1.0},
            recency_weight=0.2,
        )
        assert [r.slug for r in ranked] == ["lesson_b", "lesson_a"]

    def test_stored_relevance_is_never_the_adjusted_value(self):
        lessons = self._tied_pair()
        unadjusted = retrieval.rank_lessons("refund policy enterprise", lessons)
        adjusted = retrieval.rank_lessons(
            "refund policy enterprise", lessons,
            reliability_lookup={"lesson_a": -1.0, "lesson_b": 1.0},
            reliability_weight=0.9,
        )
        assert {r.slug: r.relevance for r in unadjusted} == {r.slug: r.relevance for r in adjusted}

    def test_adjustment_fields_are_recorded_on_the_ranked_lesson(self):
        lessons = self._tied_pair()
        ranked = retrieval.rank_lessons(
            "refund policy enterprise", lessons,
            reliability_lookup={"lesson_a": -1.0, "lesson_b": 1.0},
            reliability_weight=0.2,
            recency_lookup={"lesson_a": 0.5, "lesson_b": 0.5},
            recency_weight=0.2,
        )
        by_slug = {r.slug: r for r in ranked}
        assert by_slug["lesson_a"].reliability_adjustment == -1.0
        assert by_slug["lesson_b"].reliability_adjustment == 1.0
        assert by_slug["lesson_a"].recency_adjustment == 0.5

    def test_a_below_floor_lesson_is_not_rescued_by_a_reliable_verdict(self):
        lessons = [_lesson("lesson_off_topic", description="completely unrelated widget content")]
        ranked = retrieval.rank_lessons(
            "refund policy enterprise", lessons,
            reliability_lookup={"lesson_off_topic": 1.0},
            reliability_weight=1.0,
        )
        assert ranked == []

    def test_a_missing_slug_in_the_lookup_is_treated_as_zero_adjustment(self):
        lessons = self._tied_pair()
        ranked = retrieval.rank_lessons(
            "refund policy enterprise", lessons,
            reliability_lookup={"lesson_b": 1.0},
            reliability_weight=0.2,
        )
        assert [r.slug for r in ranked] == ["lesson_b", "lesson_a"]
        assert [r for r in ranked if r.slug == "lesson_a"][0].reliability_adjustment == 0.0

    def test_a_topically_stronger_match_still_wins_over_a_large_reliability_gap(self):
        lessons = [
            _lesson("lesson_strong_match",
                    description="refund policy for enterprise accounts",
                    applies_when="refund policy enterprise accounts dispute",
                    tags=["refund", "enterprise", "policy"], importance=3),
            _lesson("lesson_weak_match", description="refund", importance=3),
        ]
        ranked = retrieval.rank_lessons(
            "refund policy for enterprise accounts dispute", lessons,
            reliability_lookup={"lesson_strong_match": -1.0, "lesson_weak_match": 1.0},
            reliability_weight=0.2,
        )
        assert ranked[0].slug == "lesson_strong_match"


class TestApplyReranker:
    def _ranked(self):
        lessons = [
            _lesson("lesson_a", description="refund policy enterprise", importance=3),
            _lesson("lesson_b", description="refund policy enterprise", importance=1),
        ]
        return retrieval.rank_lessons("refund policy enterprise", lessons)

    def test_none_is_a_no_op_returns_the_exact_same_list(self):
        ranked = self._ranked()
        assert retrieval.apply_reranker("refund policy enterprise", ranked, None) is ranked

    def test_a_reranker_can_reverse_the_order(self):
        ranked = self._ranked()
        assert [r.slug for r in ranked] == ["lesson_a", "lesson_b"]

        def reverse_reranker(task, candidates):
            return list(reversed(candidates))

        reranked = retrieval.apply_reranker("refund policy enterprise", ranked, reverse_reranker)
        assert [r.slug for r in reranked] == ["lesson_b", "lesson_a"]

    def test_the_reranker_receives_the_task_string(self):
        ranked = self._ranked()
        seen = {}

        def spy_reranker(task, candidates):
            seen["task"] = task
            return candidates

        retrieval.apply_reranker("refund policy enterprise", ranked, spy_reranker)
        assert seen["task"] == "refund policy enterprise"

    def test_the_reranker_receives_every_candidate_the_first_stage_returned(self):
        ranked = self._ranked()
        seen = {}

        def spy_reranker(task, candidates):
            seen["slugs"] = [c.slug for c in candidates]
            return candidates

        retrieval.apply_reranker("refund policy enterprise", ranked, spy_reranker)
        assert seen["slugs"] == [r.slug for r in ranked]
