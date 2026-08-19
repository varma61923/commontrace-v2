"""Unit tests for commontrace/retrieval.py -- the pure-Python lexical
fallback retriever that makes `commontrace query` work with only the core
(PyYAML-only) install, per protocol/PROTOCOL.md §5's "Local tier remains
file-based for agents that can only read/write files"."""
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


def test_tag_match_outweighs_description_only_match():
    # tags carry weight 2.0, description carries weight 1.0 (see _lesson_text_weighted) --
    # a lesson whose *tag* matches the query should outrank one that only matches in prose.
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
    # A query of pure stopwords should tokenize to nothing and match nothing.
    assert retrieval.rank_lessons("the of and", lessons) == []
