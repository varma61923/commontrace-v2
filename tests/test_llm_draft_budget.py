from __future__ import annotations

from commontrace.commands import _llm_draft, lesson_cmd


def test_the_evidence_block_is_bounded():
    lines = [f"- line {i} " + "x" * 200 for i in range(1000)]
    built = _llm_draft.prompt("i", "s", "r", "a", "d", lines)
    assert len(built) < _llm_draft.MAX_EVIDENCE_CHARS + 2000
    assert "further evidence line(s) omitted" in built and built.rstrip().endswith("support your answer.")
    small = _llm_draft.prompt("i", "s", "r", "a", "d", ["- one"])
    assert "omitted" not in small


class _Ev:
    def __init__(self, occasion_id):
        self.occasion_id = occasion_id


def test_revision_evidence_lists_the_most_recent_occasions_per_section_and_only_those_may_be_cited():
    n = lesson_cmd.EVIDENCE_PER_SECTION
    hits = [_Ev(f"h{i}") for i in range(n + 25)]
    misses = [_Ev(f"m{i}") for i in range(3)]
    text = "\n".join(lesson_cmd._evidence_sections(hits, misses, {}))
    assert "`h0`" not in text and f"`h{n + 24}`" in text and f"({n + 25} occasions" in text
    assert all(f"`m{i}`" in text for i in range(3))
    allowed = lesson_cmd._listed_ids(hits, misses)
    assert "h0" not in allowed and f"h{n + 24}" in allowed and len(allowed) == n + 3
