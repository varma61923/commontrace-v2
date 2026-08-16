"""Tests for benchmark/measure_performance.py."""
import os
import sys
import json
import pytest

# Fixtures and helpers from conftest
from conftest import write_lesson, write_episode

# Import the benchmark module under test
import measure_performance as bm


# ---------------------------------------------------------------------------
# YAML / frontmatter parsing
# ---------------------------------------------------------------------------

class TestParseFrontmatter:
    def test_basic_parse(self):
        content = "---\nname: test\nimportance: 3\ntags: [a, b]\n---\nbody"
        result = bm.parse_frontmatter(content)
        assert result["name"] == "test"
        assert result["importance"] == 3
        assert result["tags"] == ["a", "b"]

    def test_missing_closing_delim(self):
        content = "---\nname: test\nbody"
        assert bm.parse_frontmatter(content) is None

    def test_no_frontmatter(self):
        content = "Just a body, no frontmatter"
        assert bm.parse_frontmatter(content) is None

    def test_empty_list(self):
        content = "---\nfield: []\n---\n"
        result = bm.parse_frontmatter(content)
        assert result["field"] == []

    def test_string_with_quotes(self):
        content = '---\nname: "quoted"\n---\n'
        result = bm.parse_yaml_minimal('name: "quoted"\n')
        assert result["name"] == "quoted"


# ---------------------------------------------------------------------------
# lesson_quality metric
# ---------------------------------------------------------------------------

class TestLessonQuality:
    def test_full_validation(self):
        episodes = [
            {"lessons_proposed_by_omega": ["a", "b"], "lessons_validated_by_lambda": ["a", "b"]},
        ]
        val, n = bm.compute_lesson_quality(episodes)
        assert val == pytest.approx(1.0)
        assert n == 1

    def test_partial_validation(self):
        episodes = [
            {"lessons_proposed_by_omega": ["a", "b", "c"], "lessons_validated_by_lambda": ["a"]},
        ]
        val, n = bm.compute_lesson_quality(episodes)
        assert val == pytest.approx(1 / 3)
        assert n == 1

    def test_no_proposals(self):
        episodes = [{"lessons_proposed_by_omega": []}]
        val, n = bm.compute_lesson_quality(episodes)
        assert val is None
        assert n == 0

    def test_multiple_episodes(self):
        episodes = [
            {"lessons_proposed_by_omega": ["a"], "lessons_validated_by_lambda": ["a"]},
            {"lessons_proposed_by_omega": ["b", "c"], "lessons_validated_by_lambda": []},
        ]
        val, n = bm.compute_lesson_quality(episodes)
        assert val == pytest.approx(0.5)  # mean(1.0, 0.0)
        assert n == 2

    def test_legacy_field_fallback(self):
        """lessons_validated_by_user (v2.1 field) should be read as fallback."""
        episodes = [
            {"lessons_proposed_by_omega": ["x"], "lessons_validated_by_user": ["x"]},
        ]
        val, n = bm.compute_lesson_quality(episodes)
        assert val == pytest.approx(1.0)

    def test_quality_above_100_percent(self):
        """Retro-validation artefact: validated can include slugs from other episodes."""
        episodes = [
            {"lessons_proposed_by_omega": ["a"], "lessons_validated_by_lambda": ["a", "b"]},
        ]
        val, n = bm.compute_lesson_quality(episodes)
        assert val == pytest.approx(2.0)  # 2 validated / 1 proposed


# ---------------------------------------------------------------------------
# implicit_retrieval metric
# ---------------------------------------------------------------------------

class TestImplicitRetrieval:
    def test_perfect_retrieval(self):
        episodes = [
            {"lessons_retrieved_by_alpha": ["x", "y"], "lessons_hit": ["x", "y"]},
        ]
        strict, permissive, n = bm.compute_implicit_retrieval(episodes)
        assert strict == pytest.approx(1.0)
        assert permissive == pytest.approx(1.0)
        assert n == 1

    def test_partial_hit(self):
        episodes = [
            {"lessons_retrieved_by_alpha": ["x", "y"], "lessons_hit": ["x"]},
        ]
        strict, permissive, n = bm.compute_implicit_retrieval(episodes)
        assert strict == pytest.approx(0.5)
        assert permissive == pytest.approx(0.5)

    def test_permissive_exceeds_100(self):
        """hit can include lessons not in retrieved — permissive > 1 is expected."""
        episodes = [
            {"lessons_retrieved_by_alpha": ["x"], "lessons_hit": ["x", "y", "z"]},
        ]
        strict, permissive, n = bm.compute_implicit_retrieval(episodes)
        assert strict == pytest.approx(1.0)
        assert permissive == pytest.approx(3.0)

    def test_empty_retrieved(self):
        episodes = [{"lessons_retrieved_by_alpha": [], "lessons_hit": ["x"]}]
        strict, permissive, n = bm.compute_implicit_retrieval(episodes)
        assert strict is None
        assert n == 0

    def test_no_hit(self):
        episodes = [
            {"lessons_retrieved_by_alpha": ["x", "y"], "lessons_hit": []},
        ]
        strict, permissive, n = bm.compute_implicit_retrieval(episodes)
        assert strict == pytest.approx(0.0)
        assert permissive == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# transfer_gap metric
# ---------------------------------------------------------------------------

class TestTransferGap:
    def test_single_project_no_transfer(self, tmp_memory):
        """All episodes on the same project → transfer_gap = 0."""
        write_lesson(tmp_memory, "lesson_foo", source_episodes=["ep_1"], uses=1)
        episodes = [
            {
                "name": "ep_2",
                "project": "proj-a",
                "lessons_hit": ["lesson_foo"],
            },
            {
                "name": "ep_1",
                "project": "proj-a",
            },
        ]
        lessons = {
            "lesson_foo": {
                "source_episodes": ["ep_1"],
                "_path": str(tmp_memory / "lessons" / "lesson_foo.md"),
            }
        }
        old_base = bm.BASE_DIR
        bm.BASE_DIR = str(tmp_memory)
        try:
            val, total, untraceable = bm.compute_transfer_gap(episodes, lessons)
        finally:
            bm.BASE_DIR = old_base
        assert val == pytest.approx(0.0)
        assert total == 1

    def test_cross_project_transfer(self, tmp_memory):
        """Lesson seeded on proj-a hits on proj-b → transfer_gap = 1."""
        write_lesson(tmp_memory, "lesson_bar", source_episodes=["ep_a"], uses=1)
        write_episode(tmp_memory, "ep_a", project="proj-a")

        episodes = [
            {
                "name": "ep_b",
                "project": "proj-b",
                "lessons_hit": ["lesson_bar"],
            },
        ]
        lessons = {
            "lesson_bar": {
                "source_episodes": ["ep_a"],
                "_path": str(tmp_memory / "lessons" / "lesson_bar.md"),
            }
        }
        old_base = bm.BASE_DIR
        bm.BASE_DIR = str(tmp_memory)
        try:
            val, total, untraceable = bm.compute_transfer_gap(episodes, lessons)
        finally:
            bm.BASE_DIR = old_base
        assert val == pytest.approx(1.0)
        assert total == 1

    def test_untraceable_hit(self):
        """Lesson with no source_episodes → hit is untraceable."""
        episodes = [{"name": "ep_x", "project": "p", "lessons_hit": ["lesson_baz"]}]
        lessons = {"lesson_baz": {"source_episodes": []}}
        val, total, untraceable = bm.compute_transfer_gap(episodes, lessons)
        assert val is None
        assert untraceable == 1


# ---------------------------------------------------------------------------
# Alert thresholds
# ---------------------------------------------------------------------------

class TestAlerts:
    def _make_report(self, lq_val=1.0, ir_strict=1.0, never_hit=None, n_lessons=2):
        never_hit = never_hit or []
        return {
            "lesson_quality": {"value": lq_val, "n": 1},
            "implicit_retrieval": {"strict": ir_strict, "permissive": ir_strict, "n": 1},
            "transfer_gap": {"value": 0.0, "n": 1, "untraceable": 0},
            "n_lessons": n_lessons,
            "extras": {"never_hit": never_hit, "top5": [], "proposed_not_validated": [],
                        "importance_lessons": {}, "importance_episodes": {}, "domain_coverage": {}},
        }

    def test_no_alerts_when_all_healthy(self):
        report = self._make_report()
        thresholds = {"quality": 0.8, "retrieval": 0.7, "never_hit": 0.25}
        alerts = bm.compute_alerts(report, thresholds)
        assert alerts == []

    def test_quality_alert(self):
        report = self._make_report(lq_val=0.5)
        thresholds = {"quality": 0.8, "retrieval": 0.7, "never_hit": 0.25}
        alerts = bm.compute_alerts(report, thresholds)
        assert any("lesson_quality" in a for a in alerts)

    def test_retrieval_alert(self):
        report = self._make_report(ir_strict=0.5)
        thresholds = {"quality": 0.8, "retrieval": 0.7, "never_hit": 0.25}
        alerts = bm.compute_alerts(report, thresholds)
        assert any("implicit_retrieval" in a for a in alerts)

    def test_never_hit_alert(self):
        report = self._make_report(never_hit=["a", "b"], n_lessons=2)
        thresholds = {"quality": 0.8, "retrieval": 0.7, "never_hit": 0.25}
        alerts = bm.compute_alerts(report, thresholds)
        assert any("never hit" in a for a in alerts)

    def test_quality_above_100_alert(self):
        report = self._make_report(lq_val=1.5)
        thresholds = {"quality": 0.8, "retrieval": 0.7, "never_hit": 0.25}
        alerts = bm.compute_alerts(report, thresholds)
        assert any("100%" in a for a in alerts)

    def test_no_alert_when_metrics_are_none(self):
        report = self._make_report()
        report["lesson_quality"]["value"] = None
        report["implicit_retrieval"]["strict"] = None
        thresholds = {"quality": 0.8, "retrieval": 0.7, "never_hit": 0.25}
        alerts = bm.compute_alerts(report, thresholds)
        assert alerts == []


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

class TestHtmlRendering:
    def test_headers_converted(self):
        md = "# Title\n## Section"
        html = bm._md_to_html_fragment(md)
        assert "<h1>Title</h1>" in html
        assert "<h2>Section</h2>" in html

    def test_bold_converted(self):
        html = bm._md_to_html_fragment("**bold text**")
        assert "<strong>bold text</strong>" in html

    def test_inline_code_converted(self):
        html = bm._md_to_html_fragment("`code`")
        assert "<code>code</code>" in html

    def test_table_converted(self):
        md = "| A | B |\n|---|---|\n| 1 | 2 |"
        html = bm._md_to_html_fragment(md)
        assert "<table>" in html
        assert "<th>A</th>" in html
        assert "<td>1</td>" in html

    def test_bullet_list_converted(self):
        md = "- item one\n- item two"
        html = bm._md_to_html_fragment(md)
        assert "<ul>" in html
        assert "<li>item one</li>" in html

    def test_full_render_html(self):
        html = bm.render_html("# Report\n\nContent", "2026-01-01T00:00:00")
        assert "<!DOCTYPE html>" in html
        assert "<h1>Report</h1>" in html

    def test_alerts_in_html(self):
        alerts = ["metric_x below threshold"]
        html = bm.render_html("# Report", "2026-01-01T00:00:00", alerts=alerts)
        assert "alerts" in html
        assert "metric_x below threshold" in html


# ---------------------------------------------------------------------------
# JSON schema version
# ---------------------------------------------------------------------------

class TestSchemaVersion:
    def test_schema_version_present(self):
        assert bm.SCHEMA_VERSION is not None
        parts = bm.SCHEMA_VERSION.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)


# ---------------------------------------------------------------------------
# Integration: load real example fixtures from the repo
# ---------------------------------------------------------------------------

class TestIntegrationExamples:
    """Run against the illustrative example memory included in the repo."""

    def setup_method(self):
        # Point benchmark at the repo's own memory directory
        self._old_base = bm.BASE_DIR
        bm.BASE_DIR = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "memory",
        )

    def teardown_method(self):
        bm.BASE_DIR = self._old_base

    def test_example_episode_loads(self):
        episodes = bm.load_episodes()
        assert len(episodes) >= 1
        ep = episodes[0]
        assert "name" in ep
        assert "verdict" in ep

    def test_example_episode_verdict_conform(self):
        """Example episode should have CONFORM verdict (not French CONFORME)."""
        episodes = bm.load_episodes()
        for ep in episodes:
            assert ep.get("verdict") in ("CONFORM", "ARBITRATION", "ABANDON", None), (
                f"Unexpected verdict '{ep.get('verdict')}' in episode '{ep.get('name')}'. "
                "Expected CONFORM | ARBITRATION | ABANDON."
            )

    def test_example_lessons_load(self):
        lessons = bm.load_lessons()
        assert len(lessons) >= 1

    def test_example_lesson_importance_valid(self):
        lessons = bm.load_lessons()
        for slug, l in lessons.items():
            if "importance" in l:
                assert isinstance(l["importance"], int), f"{slug}: importance must be int"
                assert 1 <= l["importance"] <= 5, f"{slug}: importance must be 1-5"

    def test_benchmark_runs_without_error(self):
        episodes = bm.load_episodes()
        lessons = bm.load_lessons()
        lq, lq_n = bm.compute_lesson_quality(episodes)
        ir_s, ir_p, ir_n = bm.compute_implicit_retrieval(episodes)
        tg, tg_n, tg_u = bm.compute_transfer_gap(episodes, lessons)
        extras = bm.compute_extras(episodes, lessons)
        assert lq is None or (0.0 <= lq)  # lesson_quality can exceed 1.0 (retro-validation)
