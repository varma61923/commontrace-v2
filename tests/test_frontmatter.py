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

    def test_boolean_and_null(self):
        text = "resolved: true\nescalated: false\nhub_trace_id: null\n"
        result = bm.parse_yaml_minimal(text)
        assert result["resolved"] is True
        assert result["escalated"] is False
        assert result["hub_trace_id"] is None

    def test_nested_mapping(self):
        """PyYAML block-dumps a nested dict as 'key:\\n  subkey: value', not inline."""
        text = "outcome:\n  resolved: true\n  tokens_used: 100\n  baseline: false\n"
        result = bm.parse_yaml_minimal(text)
        assert result["outcome"] == {"resolved": True, "tokens_used": 100, "baseline": False}

    def test_block_list_of_scalars(self):
        """PyYAML's actual default output style for a list is block ('- item'), not '[a, b]'."""
        text = "tags:\n- refunds\n- tone\n"
        result = bm.parse_yaml_minimal(text)
        assert result["tags"] == ["refunds", "tone"]

    def test_block_list_of_dicts(self):
        text = "importance_history:\n- date: '2026-01-01'\n  old: 3\n  new: 4\n  reason: r\n"
        result = bm.parse_yaml_minimal(text)
        assert result["importance_history"] == [
            {"date": "2026-01-01", "old": 3, "new": 4, "reason": "r"}
        ]

    def test_keys_after_a_block_list_are_not_dropped(self):
        """Regression: a block-list value must not swallow the rest of the mapping."""
        text = "tags:\n- a\n- b\nagent_type: support\nstatus: active\n"
        result = bm.parse_yaml_minimal(text)
        assert result == {"tags": ["a", "b"], "agent_type": "support", "status": "active"}

    def test_trailing_inline_comment_stripped(self):
        text = "agent_type: code         # REQUIRED, open vocabulary: code | support\n"
        result = bm.parse_yaml_minimal(text)
        assert result["agent_type"] == "code"

    def test_trailing_comment_on_empty_list_does_not_corrupt_value(self):
        text = 'importance_history: []   # log of changes: [{date: YYYY-MM-DD, reason: "..."}]\n'
        result = bm.parse_yaml_minimal(text)
        assert result["importance_history"] == []

    def test_hash_inside_quotes_is_not_a_comment(self):
        text = 'title: "before # after"\n'
        result = bm.parse_yaml_minimal(text)
        assert result["title"] == "before # after"

    def test_pyyaml_line_wrapped_long_value_is_not_truncated(self):
        """Regression: PyYAML wraps scalars > width=80 across continuation lines. The
        parser used to stop at the first line without a 'key:' pattern, truncating the
        value AND silently dropping every key after it."""
        text = (
            "importance_rationale: Prevented a force-push that would have destroyed two "
            "days of reviewer work.\n"
            "applies_when: When the agent is about to run git push --force on a shared "
            "branch without confirming with the team first.\n"
            "uses: 0\nstatus: active\n"
        )
        # Reproduce PyYAML's actual wrapping via safe_dump so this is testing the real shape.
        import yaml as _yaml
        wrapped = _yaml.safe_dump(_yaml.safe_load(text), sort_keys=False)
        result = bm.parse_yaml_minimal(wrapped)
        assert result["uses"] == 0
        assert result["status"] == "active"
        assert "reviewer work." in result["importance_rationale"]
        assert "team first." in result["applies_when"]

    def test_colon_without_trailing_space_is_not_a_key(self):
        """Regression: a bare colon inside a list-item scalar (e.g. a URL or ratio) with
        no following space must not be misread as a nested 'key: value'."""
        text = "tags:\n- see http://example.com:8080/path for details\n- ratio 3:1\n"
        result = bm.parse_yaml_minimal(text)
        assert result["tags"] == [
            "see http://example.com:8080/path for details",
            "ratio 3:1",
        ]

    def test_quoted_list_item_containing_colon_space(self):
        """Regression: a quoted scalar list item containing ': ' (why PyYAML quoted it)
        must not have the internal colon misread as a mapping key."""
        text = "items:\n- 'first part: second part'\n- plain\n"
        result = bm.parse_yaml_minimal(text)
        assert result["items"] == ["first part: second part", "plain"]

    def test_scientific_notation_requires_a_decimal_point(self):
        """Regression: matches PyYAML's own resolver -- '7E3' (no dot) stays a string,
        only a form with a literal '.' in the mantissa (e.g. '7.0e3') is a float."""
        assert bm.parse_yaml_minimal("a: 7E3\n")["a"] == "7E3"
        assert bm.parse_yaml_minimal("a: 7.0e3\n")["a"] == 7.0e3

    def test_flow_list_of_dicts_not_shredded_by_naive_comma_split(self):
        # date is an unquoted YAML date scalar -- real PyYAML resolves it to
        # datetime.date too (confirmed against yaml.safe_load), not a string.
        import datetime as _dt

        text = "importance_history: [{date: 2026-01-01, old: 3, new: 4, reason: bumped}]\n"
        result = bm.parse_yaml_minimal(text)
        assert result["importance_history"] == [
            {"date": _dt.date(2026, 1, 1), "old": 3, "new": 4, "reason": "bumped"}
        ]

    def test_hyphenated_and_numeric_keys_do_not_break_the_whole_document(self):
        text = "agent-type: support\n2026: x\nafter: y\n"
        result = bm.parse_yaml_minimal(text)
        assert result["agent-type"] == "support"
        assert result["after"] == "y"

    def test_round_trips_against_real_pyyaml_output(self):
        """The fallback parser must agree with real PyYAML on this project's own frontmatter shape."""
        import yaml as _yaml

        text = (
            "id: id1\n"
            "title: 'A title: with colon'\n"
            "tags:\n- refunds\n- tone\n"
            "outcome:\n  resolved: true\n  tokens_used: 100\n  baseline: false\n"
            "hub_trace_id: null\n"
        )
        assert bm.parse_yaml_minimal(text) == _yaml.safe_load(text)


class TestFrontmatterDelimiterHandling:
    """commontrace/frontmatter.py must split on '---' delimiter LINES, not the substring
    '---' anywhere in the file -- an ordinary title/description containing '---' should
    not corrupt every other field.
    """

    def test_field_value_containing_triple_dash_does_not_corrupt_parse(self, tmp_path):
        from commontrace import frontmatter

        path = tmp_path / "trace.md"
        frontmatter.write(
            str(path),
            {"id": "x", "title": "before---after marker", "agent_type": "code"},
            "body text",
        )
        fm, body = frontmatter.read(str(path))
        assert fm["title"] == "before---after marker"
        assert fm["id"] == "x"
        assert fm["agent_type"] == "code"
        assert body.strip() == "body text"

    def test_round_trip_with_colon_and_quotes(self, tmp_path):
        from commontrace import frontmatter

        path = tmp_path / "trace2.md"
        fm_in = {"id": "y", "title": 'A "quoted" title: with colon', "agent_type": "code"}
        frontmatter.write(str(path), fm_in, "body")
        fm_out, _ = frontmatter.read(str(path))
        assert fm_out["title"] == fm_in["title"]


class TestFrontmatterMalformedInput:
    """commontrace/frontmatter.py must raise a clean, catchable error on hostile or
    malformed frontmatter -- never an uncaught yaml.YAMLError/AttributeError traceback.
    """

    def test_invalid_yaml_syntax_raises_frontmatter_error(self, tmp_path):
        from commontrace import frontmatter

        path = tmp_path / "bad.md"
        path.write_text("---\nkey: [unclosed\n---\nbody\n", encoding="utf-8")
        with pytest.raises(frontmatter.FrontmatterError):
            frontmatter.read(str(path))

    def test_frontmatter_error_is_a_value_error(self, tmp_path):
        """Callers doing a broad `except ValueError` (a natural instinct for bad input)
        must still catch this."""
        from commontrace import frontmatter

        assert issubclass(frontmatter.FrontmatterError, ValueError)

    def test_top_level_scalar_raises_frontmatter_error(self, tmp_path):
        """A frontmatter block that parses to a plain string, not a mapping, must not
        silently become an object callers then call `.get(...)` on."""
        from commontrace import frontmatter

        path = tmp_path / "scalar.md"
        path.write_text("---\njust a plain scalar\n---\nbody\n", encoding="utf-8")
        with pytest.raises(frontmatter.FrontmatterError):
            frontmatter.read(str(path))

    def test_top_level_list_raises_frontmatter_error(self, tmp_path):
        from commontrace import frontmatter

        path = tmp_path / "list.md"
        path.write_text("---\n- a\n- b\n---\nbody\n", encoding="utf-8")
        with pytest.raises(frontmatter.FrontmatterError):
            frontmatter.read(str(path))

    def test_empty_frontmatter_block_returns_empty_dict(self, tmp_path):
        """An empty block (---\\n---\\n) is valid YAML (None) and should stay a
        no-op empty mapping, not an error."""
        from commontrace import frontmatter

        path = tmp_path / "empty.md"
        path.write_text("---\n---\nbody\n", encoding="utf-8")
        fm, body = frontmatter.read(str(path))
        assert fm == {}
        assert body.strip() == "body"


class TestTraceIoSectionParsing:
    """commontrace/trace_io.py must not truncate Context/Solution at an unrelated '## '
    sub-heading embedded inside the section's own text.
    """

    def test_solution_with_embedded_subheading_is_not_truncated(self, tmp_path):
        from commontrace import frontmatter, trace_io

        path = tmp_path / "trace.md"
        solution = "Step one\n## Substep heading\nThis part must not be lost\nMore text"
        frontmatter.write(
            str(path),
            {"id": "x", "title": "t", "agent_type": "code"},
            f"## Context\nctx\n\n## Solution\n{solution}\n",
        )
        instance, _ = trace_io.read(str(path))
        assert instance["solution_text"] == solution


class TestLessonFrontmatterRequiredFields:
    """Verify that real lesson template files satisfy expected schema."""

    REQUIRED_FIELDS = [
        "name", "description", "tags", "agent_type", "domain", "importance",
        "importance_rationale", "applies_when", "do_not_apply_when",
        "uses", "last_hit", "source_traces", "status",
    ]

    # `domain` is an open vocabulary at the protocol level (protocol/PROTOCOL.md#7-taxonomy-open-not-closed).
    # This is the code-review profile's historical starter set — informational only,
    # NOT enforced as a closed list. See test_example_lessons_domain_is_nonempty_string.
    CODE_PROFILE_DOMAINS = {
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

    def test_example_lessons_domain_is_nonempty_string(self):
        """`domain` is open vocabulary (protocol/PROTOCOL.md#7-taxonomy-open-not-closed) — only shape is checked."""
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        import glob
        lessons_dir = os.path.join(repo_root, "memory", "lessons")
        for path in glob.glob(os.path.join(lessons_dir, "lesson_*.md")):
            if path.endswith("lesson_template.md"):
                continue
            fm = self._load_lesson(path)
            domain = fm.get("domain")
            assert isinstance(domain, str) and domain, (
                f"{os.path.basename(path)}: domain must be a non-empty string, got {domain!r}"
            )

    def test_example_lessons_have_agent_type(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        import glob
        lessons_dir = os.path.join(repo_root, "memory", "lessons")
        for path in glob.glob(os.path.join(lessons_dir, "lesson_*.md")):
            if path.endswith("lesson_template.md"):
                continue
            fm = self._load_lesson(path)
            agent_type = fm.get("agent_type")
            assert isinstance(agent_type, str) and agent_type, (
                f"{os.path.basename(path)}: agent_type must be a non-empty string, got {agent_type!r}"
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
        "name", "description", "agent_type", "task_invocation", "tags", "project", "verdict",
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
