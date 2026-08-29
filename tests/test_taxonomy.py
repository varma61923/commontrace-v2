"""Tests for commontrace/taxonomy.py and `commontrace taxonomy` -- the
read-only "map the issues" leave-behind, as distinct from `commontrace
distill` (which writes candidate lessons and excludes already-curated
traces). Taxonomy must show the whole map, including patterns already
covered by an active lesson.
"""
from __future__ import annotations

import json

import pytest

from commontrace import distill, taxonomy
from commontrace.cli import main


def _trace(id, title, context_text, tags=None, agent_type="support"):
    return distill.TraceCandidate(
        id=id, path=f"/tmp/{id}.md", title=title, context_text=context_text,
        solution_text="fix", tags=tags or [], agent_type=agent_type,
    )


def _lesson(name, domain, source_traces, tags=None, status="active"):
    return {
        "name": name, "domain": domain, "tags": tags or [],
        "source_traces": source_traces, "status": status,
    }


REFUND_A = _trace("t1", "Refund confusion A", "customer confused about refund timeline contradictory docs")
REFUND_B = _trace("t2", "Refund confusion B", "customer confused about refund timeline contradictory docs")
PASSWORD = _trace("t3", "Password reset loop", "user stuck in password reset loop link expired")


class TestBuildTaxonomy:
    def test_groups_a_repeated_pattern_as_a_gap_with_no_lesson(self):
        tax = taxonomy.build_taxonomy([REFUND_A, REFUND_B], lessons=[])
        assert tax.n_patterns == 1
        assert tax.n_covered == 0
        assert tax.n_gaps == 1
        pattern = tax.domains[0].patterns[0]
        assert pattern.covered is False
        assert pattern.lesson_slug is None
        assert pattern.n_traces == 2

    def test_pattern_already_referenced_by_an_active_lesson_is_covered(self):
        """Unlike `distill`, taxonomy does NOT exclude traces an existing
        lesson already covers -- it must still show up, marked covered."""
        lessons = [_lesson("lesson_refund_window", "refunds", ["t1", "t2"])]
        tax = taxonomy.build_taxonomy([REFUND_A, REFUND_B], lessons)
        assert tax.n_patterns == 1
        assert tax.n_covered == 1
        assert tax.n_gaps == 0
        pattern = tax.domains[0].patterns[0]
        assert pattern.covered is True
        assert pattern.lesson_slug == "lesson_refund_window"

    def test_lesson_domain_wins_over_the_proposed_domain_when_covered(self):
        lessons = [_lesson("lesson_refund_window", "custom-domain-name", ["t1", "t2"])]
        tax = taxonomy.build_taxonomy([REFUND_A, REFUND_B], lessons)
        assert tax.domains[0].domain == "custom-domain-name"

    def test_non_active_lesson_does_not_count_as_coverage(self):
        lessons = [_lesson("lesson_x", "refunds", ["t1", "t2"], status="review")]
        tax = taxonomy.build_taxonomy([REFUND_A, REFUND_B], lessons)
        assert tax.n_covered == 0
        assert tax.domains[0].patterns[0].covered is False

    def test_unclustered_traces_are_counted_but_not_a_pattern(self):
        tax = taxonomy.build_taxonomy([REFUND_A, REFUND_B, PASSWORD], lessons=[])
        assert tax.n_traces_total == 3
        assert tax.n_patterns == 1  # PASSWORD alone never reaches min_cluster_size
        assert tax.n_unclustered == 1

    def test_empty_input_yields_empty_taxonomy(self):
        tax = taxonomy.build_taxonomy([], lessons=[])
        assert tax.domains == []
        assert tax.n_patterns == 0
        assert tax.n_traces_total == 0

    def test_two_distinct_patterns_are_both_reported_under_their_own_domains(self):
        refund_a = _trace("r1", "Refund confusion A",
                           "customer confused about refund timeline contradictory docs", tags=["refunds"])
        refund_b = _trace("r2", "Refund confusion B",
                           "customer confused about refund timeline contradictory docs", tags=["refunds"])
        password_a = _trace("p1", "Password reset loop A",
                             "user stuck in password reset loop link expired", tags=["troubleshooting"])
        password_b = _trace("p2", "Password reset loop B",
                             "user stuck in password reset loop link expired", tags=["troubleshooting"])
        tax = taxonomy.build_taxonomy([refund_a, refund_b, password_a, password_b], lessons=[])
        assert tax.n_patterns == 2
        assert len(tax.domains) == 2
        assert {g.domain for g in tax.domains} == {"refunds", "troubleshooting"}


class TestRenderMarkdown:
    def test_lists_covered_and_gap_status(self):
        lessons = [_lesson("lesson_refund_window", "refunds", ["t1", "t2"])]
        tax = taxonomy.build_taxonomy([REFUND_A, REFUND_B], lessons)
        md = taxonomy.render_markdown(tax)
        assert "covered by `lesson_refund_window`" in md

    def test_gap_is_flagged_as_needing_a_lesson(self):
        tax = taxonomy.build_taxonomy([REFUND_A, REFUND_B], lessons=[])
        md = taxonomy.render_markdown(tax)
        assert "gap — no lesson yet" in md

    def test_empty_taxonomy_explains_why(self):
        tax = taxonomy.build_taxonomy([], lessons=[])
        md = taxonomy.render_markdown(tax)
        assert "No repeated pattern found" in md


class TestRenderHtml:
    def test_escapes_untrusted_trace_tags(self):
        """Tags are free-form, attacker-controllable trace frontmatter (protocol/
        schemas/trace.schema.json), not code -- an unescaped '<script>' tag
        would be a stored XSS in the generated dashboard."""
        evil = _trace("evil1", "Shared pattern A", "shared payload words repeated words shared",
                       tags=["<script>alert(1)</script>"])
        evil2 = _trace("evil2", "Shared pattern B", "shared payload words repeated words shared",
                        tags=["<script>alert(1)</script>"])
        tax = taxonomy.build_taxonomy([evil, evil2], lessons=[])
        html_out = taxonomy.render_html(tax, "2026-01-01T00:00:00")
        assert "<script>alert" not in html_out
        assert "&lt;script&gt;" in html_out

    def test_escapes_untrusted_lesson_slug_in_covered_badge(self):
        evil_lessons = [_lesson("<script>alert(2)</script>", "refunds", ["t1", "t2"])]
        tax = taxonomy.build_taxonomy([REFUND_A, REFUND_B], evil_lessons)
        html_out = taxonomy.render_html(tax, "2026-01-01T00:00:00")
        assert "<script>alert" not in html_out
        assert "&lt;script&gt;" in html_out

    def test_produces_a_full_html_document(self):
        tax = taxonomy.build_taxonomy([REFUND_A, REFUND_B], lessons=[])
        out = taxonomy.render_html(tax, "2026-01-01T00:00:00")
        assert out.startswith("<!DOCTYPE html>")
        assert "<title>" in out


class TestToDict:
    def test_includes_computed_domain_trace_count(self):
        tax = taxonomy.build_taxonomy([REFUND_A, REFUND_B], lessons=[])
        d = taxonomy.to_dict(tax)
        assert d["domains"][0]["n_traces"] == 2
        assert json.dumps(d)  # round-trips through json with no TypeError


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMONTRACE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write_trace_file(root, name, title, context, tags, agent_type="support"):
    tdir = root / "memory" / "traces"
    tdir.mkdir(parents=True, exist_ok=True)
    tag_line = "[" + ", ".join(tags) + "]"
    (tdir / f"{name}.md").write_text(
        "---\n"
        f"id: {name}\n"
        f"title: {title}\n"
        f"agent_type: {agent_type}\n"
        f"tags: {tag_line}\n"
        "---\n\n"
        f"## Context\n{context}\n\n## Solution\nfix\n",
        encoding="utf-8",
    )


class TestTaxonomyCLI:
    def test_no_traces_says_so_and_exits_zero(self, store, capsys):
        (store / "memory" / "traces").mkdir(parents=True)
        assert main(["taxonomy", "--dest", str(store)]) == 0
        assert "nothing to map" in capsys.readouterr().out

    def test_reports_a_pattern_from_written_traces(self, store, capsys):
        _write_trace_file(store, "t1", "Refund confusion A",
                           "customer confused about refund timeline contradictory docs", ["refunds"])
        _write_trace_file(store, "t2", "Refund confusion B",
                           "customer confused about refund timeline contradictory docs", ["refunds"])
        capsys.readouterr()
        assert main(["taxonomy", "--dest", str(store)]) == 0
        out = capsys.readouterr().out
        assert "Recurring patterns found: **1**" in out

    def test_json_output_round_trips(self, store, capsys):
        _write_trace_file(store, "t1", "Refund confusion A",
                           "customer confused about refund timeline contradictory docs", ["refunds"])
        _write_trace_file(store, "t2", "Refund confusion B",
                           "customer confused about refund timeline contradictory docs", ["refunds"])
        capsys.readouterr()
        assert main(["taxonomy", "--dest", str(store), "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["n_patterns"] == 1

    def test_html_writes_a_report_file(self, store, capsys):
        _write_trace_file(store, "t1", "Refund confusion A",
                           "customer confused about refund timeline contradictory docs", ["refunds"])
        _write_trace_file(store, "t2", "Refund confusion B",
                           "customer confused about refund timeline contradictory docs", ["refunds"])
        capsys.readouterr()
        assert main(["taxonomy", "--dest", str(store), "--html"]) == 0
        out = capsys.readouterr().out
        assert "HTML report written" in out
        reports = list((store / "memory" / "benchmark_reports").glob("taxonomy_*.html"))
        assert len(reports) == 1
        assert reports[0].read_text(encoding="utf-8").startswith("<!DOCTYPE html>")

    def test_json_and_html_together_is_rejected(self, store, capsys):
        (store / "memory" / "traces").mkdir(parents=True)
        assert main(["taxonomy", "--dest", str(store), "--json", "--html"]) == 2
        assert "mutually exclusive" in capsys.readouterr().err
