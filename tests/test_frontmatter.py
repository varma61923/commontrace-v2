"""Tests for frontmatter parsing utilities (benchmark + query fallback parsers)."""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "benchmark"))
import measure_performance as bm


class TestMinimalYamlParser:
    """Tests for parse_yaml_minimal (stdlib fallback, no PyYAML dependency)."""

    def test_string_field(self):
        text = "name: my-lesson\n"
        result = bm.parse_yaml_minimal(text)
        assert result["name"] == "my-lesson"

    def test_integer_field(self):
        text = "importance: 4\n"
        result = bm.parse_yaml_minimal(text)
        assert result["importance"] == 4
        assert isinstance(result["importance"], int)

    def test_negative_integer(self):
        text = "value: -1\n"
        result = bm.parse_yaml_minimal(text)
        assert result["value"] == -1

    def test_quoted_string(self):
        text = 'desc: "hello world"\n'
        result = bm.parse_yaml_minimal(text)
        assert result["desc"] == "hello world"

    def test_single_quoted_string(self):
        text = "desc: 'hello world'\n"
        result = bm.parse_yaml_minimal(text)
        assert result["desc"] == "hello world"

    def test_empty_list(self):
        text = "tags: []\n"
        result = bm.parse_yaml_minimal(text)
        assert result["tags"] == []

    def test_list_with_values(self):
        text = "tags: [a, b, c]\n"
        result = bm.parse_yaml_minimal(text)
        assert result["tags"] == ["a", "b", "c"]

    def test_comment_lines_ignored(self):
        text = "# This is a comment\nname: test\n"
        result = bm.parse_yaml_minimal(text)
        assert "name" in result
        assert len(result) == 1

    def test_empty_text(self):
        result = bm.parse_yaml_minimal("")
        assert result == {}

    def test_unknown_field_type_treated_as_string(self):
        text = "status: active\n"
        result = bm.parse_yaml_minimal(text)
        assert result["status"] == "active"


class TestLessonFrontmatterRequiredFields:
    """Verify that real lesson template files satisfy expected schema."""

    REQUIRED_FIELDS = [
        "name", "description", "tags", "domain", "importance",
        "importance_rationale", "applies_when", "do_not_apply_when",
        "uses", "last_hit", "source_episodes", "status",
    ]

    VALID_DOMAINS = {
        "git-safety", "cuda-gpu", "refactor", "testing",
        "subagents", "performance", "other",
    }

    VALID_STATUSES = {"active", "review", "archived"}

    def _load_lesson(self, path):
        with open(path) as fh:
            content = fh.read()
        return bm.parse_frontmatter(content)

    def test_lesson_template_parseable(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        template = os.path.join(repo_root, "memory", "lessons", "lesson_template.md")
        fm = self._load_lesson(template)
        assert fm is not None

    def test_example_lessons_have_required_fields(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        import glob
        lessons_dir = os.path.join(repo_root, "memory", "lessons")
        for path in glob.glob(os.path.join(lessons_dir, "lesson_*.md")):
            if path.endswith("lesson_template.md"):
                continue
            fm = self._load_lesson(path)
            assert fm is not None, f"Could not parse {path}"
            for field in self.REQUIRED_FIELDS:
                assert field in fm, f"Missing '{field}' in {os.path.basename(path)}"

    def test_example_lessons_domain_valid(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        import glob
        lessons_dir = os.path.join(repo_root, "memory", "lessons")
        for path in glob.glob(os.path.join(lessons_dir, "lesson_*.md")):
            if path.endswith("lesson_template.md"):
                continue
            fm = self._load_lesson(path)
            domain = fm.get("domain")
            assert domain in self.VALID_DOMAINS, (
                f"{os.path.basename(path)}: domain '{domain}' not in {self.VALID_DOMAINS}"
            )

    def test_example_lessons_status_valid(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        import glob
        lessons_dir = os.path.join(repo_root, "memory", "lessons")
        for path in glob.glob(os.path.join(lessons_dir, "lesson_*.md")):
            if path.endswith("lesson_template.md"):
                continue
            fm = self._load_lesson(path)
            status = fm.get("status")
            assert status in self.VALID_STATUSES, (
                f"{os.path.basename(path)}: status '{status}' not in {self.VALID_STATUSES}"
            )

    def test_example_lessons_importance_range(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        import glob
        lessons_dir = os.path.join(repo_root, "memory", "lessons")
        for path in glob.glob(os.path.join(lessons_dir, "lesson_*.md")):
            if path.endswith("lesson_template.md"):
                continue
            fm = self._load_lesson(path)
            imp = fm.get("importance")
            if imp is None:
                continue  # missing importance handled gracefully (defaults to 3)
            assert isinstance(imp, int), f"{path}: importance must be int, got {type(imp)}"
            assert 1 <= imp <= 5, f"{path}: importance {imp} out of range [1,5]"


class TestEpisodeFrontmatterRequiredFields:
    """Verify that real episode files satisfy expected schema."""

    REQUIRED_FIELDS = [
        "name", "description", "task_invocation", "tags", "project", "verdict",
        "importance", "n_iterations", "commit_sha", "duration_minutes",
        "lessons_retrieved_by_alpha", "lessons_hit",
        "lessons_proposed_by_omega", "lessons_validated_by_lambda",
    ]

    VALID_VERDICTS = {"CONFORM", "ARBITRATION", "ABANDON"}

    def _load_episode(self, path):
        with open(path) as fh:
            content = fh.read()
        return bm.parse_frontmatter(content)

    def test_episode_template_parseable(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        template = os.path.join(repo_root, "memory", "episodes", "episode_template.md")
        fm = self._load_episode(template)
        assert fm is not None

    def test_episode_template_verdict_is_conform(self):
        """Template verdict must be CONFORM (not CONFORME) to guide users correctly."""
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        template = os.path.join(repo_root, "memory", "episodes", "episode_template.md")
        fm = self._load_episode(template)
        assert fm is not None
        assert fm.get("verdict") in self.VALID_VERDICTS, (
            f"episode_template verdict '{fm.get('verdict')}' not in {self.VALID_VERDICTS}. "
            "Use CONFORM | ARBITRATION | ABANDON (English, not French)."
        )

    def test_example_episodes_verdict_valid(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        import glob
        episodes_dir = os.path.join(repo_root, "memory", "episodes")
        for path in glob.glob(os.path.join(episodes_dir, "2*.md")):
            fm = self._load_episode(path)
            assert fm is not None
            verdict = fm.get("verdict")
            assert verdict in self.VALID_VERDICTS, (
                f"{os.path.basename(path)}: verdict '{verdict}' not in {self.VALID_VERDICTS}"
            )
