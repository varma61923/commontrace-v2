"""Comprehensive unit tests for commontrace.validate JSON Schema validator and schemas."""
import pytest

from commontrace.validate import (
    UnsupportedSchemaError,
    _check_type,
    assert_supported_schema,
    load_schema,
    validate,
)

# ============================================================================
# 1. load_schema Boundary and Security Tests
# ============================================================================

class TestLoadSchema:
    def test_load_valid_trace_schema(self):
        schema = load_schema("trace.schema.json")
        assert isinstance(schema, dict)
        assert schema.get("title") == "CommonTrace Trace"
        assert "required" in schema
        assert "properties" in schema

    def test_load_valid_lesson_schema(self):
        schema = load_schema("lesson.schema.json")
        assert isinstance(schema, dict)
        assert schema.get("title") == "CommonTrace Lesson"
        assert "required" in schema
        assert "properties" in schema

    def test_load_schema_rejects_non_string(self):
        for invalid_arg in [None, 123, ["trace.schema.json"], {"name": "trace.schema.json"}]:
            with pytest.raises(ValueError, match="Invalid or unsafe schema file name"):
                load_schema(invalid_arg)  # type: ignore

    def test_load_schema_rejects_path_separators(self):
        # Forward slash, backslash, colon (Windows drive letters or stream separators)
        unsafe_names = [
            "../trace.schema.json",
            "subdir/trace.schema.json",
            "/etc/passwd",
            "..\\trace.schema.json",
            "subdir\\trace.schema.json",
            "C:trace.schema.json",
            "D:\\trace.schema.json",
            "foo/bar.json",
        ]
        for name in unsafe_names:
            with pytest.raises(ValueError, match="Invalid or unsafe schema file name"):
                load_schema(name)

    def test_load_schema_rejects_non_json_extension(self):
        unsafe_extensions = [
            "trace.yaml",
            "trace.yml",
            "trace.txt",
            "trace.py",
            "trace_schema",
            "schema.json.bak",
        ]
        for name in unsafe_extensions:
            with pytest.raises(ValueError, match="Invalid or unsafe schema file name"):
                load_schema(name)

    def test_load_schema_nonexistent_file_raises_filenotfound(self):
        with pytest.raises(FileNotFoundError):
            load_schema("nonexistent_schema_xyz_123.schema.json")


# ============================================================================
# 2. Type Checking and Type Mapping Tests
# ============================================================================

class TestTypeChecking:
    @pytest.mark.parametrize(
        "val,expected_type,expected_result",
        [
            ("hello", "string", True),
            (123, "string", False),
            (123, "integer", True),
            (12.34, "integer", False),
            (12.34, "number", True),
            (123, "number", True),
            (True, "boolean", True),
            (False, "boolean", True),
            ([], "array", True),
            ([1, 2], "array", True),
            ({}, "object", True),
            ({"a": 1}, "object", True),
            (None, "null", True),
            ("hello", "null", False),
        ],
    )
    def test_check_type_primitives(self, val, expected_type, expected_result):
        assert _check_type(val, expected_type) is expected_result

    def test_bool_not_accepted_as_integer_or_number(self):
        """In Python, bool is a subclass of int. Verify JSON Schema rules disallow True/False as int/number."""
        assert not _check_type(True, "integer")
        assert not _check_type(False, "integer")
        assert not _check_type(True, "number")
        assert not _check_type(False, "number")

    def test_union_types(self):
        assert _check_type(True, ["boolean", "null"])
        assert _check_type(None, ["boolean", "null"])
        assert not _check_type(123, ["boolean", "null"])

        assert _check_type(100, ["integer", "null"])
        assert _check_type(None, ["integer", "null"])
        assert not _check_type(True, ["integer", "null"])


# ============================================================================
# 3. Supported Keywords and Constraint Enforcement
# ============================================================================

class TestKeywordValidation:
    def test_validate_non_dict_instance(self):
        errors = validate(["not", "a", "dict"], {"type": "object"})  # type: ignore
        assert errors == ["Instance must be a dictionary / JSON object"]
        errors = validate("not a dict", {"type": "object"})  # type: ignore
        assert errors == ["Instance must be a dictionary / JSON object"]

    def test_required_keyword(self):
        schema = {
            "type": "object",
            "required": ["id", "title"],
            "properties": {
                "id": {"type": "string"},
                "title": {"type": "string"},
            },
        }
        # Missing both
        errs = validate({}, schema)
        assert len(errs) == 2
        assert any("missing required field 'id'" in e for e in errs)
        assert any("missing required field 'title'" in e for e in errs)

        # Missing one
        errs = validate({"id": "123"}, schema)
        assert len(errs) == 1
        assert "missing required field 'title'" in errs[0]

        # Valid
        assert validate({"id": "123", "title": "Test"}, schema) == []

    def test_enum_keyword(self):
        schema = {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["active", "review", "archived"]},
            },
        }
        assert validate({"status": "active"}, schema) == []
        assert validate({"status": "review"}, schema) == []
        assert validate({"status": "archived"}, schema) == []

        errs = validate({"status": "deleted"}, schema)
        assert len(errs) == 1
        assert "'status': 'deleted' not in enum" in errs[0]

        errs = validate({"status": 123}, schema)
        assert len(errs) == 1
        assert "expected type string, got int" in errs[0]

    def test_minimum_and_maximum_keywords(self):
        schema = {
            "type": "object",
            "properties": {
                "score": {"type": "integer", "minimum": 1, "maximum": 5},
                "ratio": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            },
        }
        # In range
        assert validate({"score": 1, "ratio": 0.0}, schema) == []
        assert validate({"score": 5, "ratio": 1.0}, schema) == []
        assert validate({"score": 3, "ratio": 0.5}, schema) == []

        # Below minimum
        errs = validate({"score": 0}, schema)
        assert any("'score': 0 < minimum 1" in e for e in errs)

        # Above maximum
        errs = validate({"score": 6}, schema)
        assert any("'score': 6 > maximum 5" in e for e in errs)

        # Ratio out of range
        errs = validate({"ratio": -0.1}, schema)
        assert any("'ratio': -0.1 < minimum 0.0" in e for e in errs)
        errs = validate({"ratio": 1.05}, schema)
        assert any("'ratio': 1.05 > maximum 1.0" in e for e in errs)

    def test_minlength_and_maxlength_keywords(self):
        schema = {
            "type": "object",
            "properties": {
                "tag": {"type": "string", "minLength": 2, "maxLength": 8},
            },
        }
        assert validate({"tag": "ab"}, schema) == []
        assert validate({"tag": "12345678"}, schema) == []

        errs = validate({"tag": "a"}, schema)
        assert any("shorter than minLength=2" in e for e in errs)

        errs = validate({"tag": "toolongstring"}, schema)
        assert any("longer than maxLength=8" in e for e in errs)

    def test_items_keyword_in_array(self):
        schema = {
            "type": "object",
            "properties": {
                "tags": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 2},
                },
            },
        }
        assert validate({"tags": []}, schema) == []
        assert validate({"tags": ["python", "testing"]}, schema) == []

        errs = validate({"tags": ["a", "valid"]}, schema)
        assert any("'tags[0]': string shorter than minLength=2" in e for e in errs)

        errs = validate({"tags": ["valid", 123]}, schema)
        assert any("'tags[1]': expected type string, got int" in e for e in errs)

    def test_nested_object_properties_and_required(self):
        schema = {
            "type": "object",
            "properties": {
                "outcome": {
                    "type": "object",
                    "required": ["resolved"],
                    "properties": {
                        "resolved": {"type": ["boolean", "null"]},
                        "tokens_used": {"type": ["integer", "null"], "minimum": 0},
                    },
                },
            },
        }
        assert validate({"outcome": {"resolved": True, "tokens_used": 100}}, schema) == []
        assert validate({"outcome": {"resolved": None, "tokens_used": None}}, schema) == []

        # Missing nested required
        errs = validate({"outcome": {"tokens_used": 50}}, schema)
        assert any("missing required field 'resolved'" in e for e in errs)

        # Negative tokens
        errs = validate({"outcome": {"resolved": True, "tokens_used": -5}}, schema)
        assert any("-5 < minimum 0" in e for e in errs)

    def test_null_value_when_null_not_allowed(self):
        schema = {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
            },
        }
        errs = validate({"title": None}, schema)
        assert any("'title': null not allowed" in e for e in errs)


# ============================================================================
# 4. assert_supported_schema and UnsupportedSchemaError Tests
# ============================================================================

class TestAssertSupportedSchema:
    def test_supported_schema_passes(self):
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "Example",
            "type": "object",
            "required": ["a"],
            "properties": {
                "a": {"type": "string", "minLength": 1, "maxLength": 10},
                "b": {"type": "integer", "minimum": 0, "maximum": 100},
                "c": {"type": "string", "enum": ["x", "y"]},
                "d": {"type": "array", "items": {"type": "string"}},
                "e": {"type": "object", "additionalProperties": True},
            },
        }
        assert_supported_schema(schema)  # should not raise

    def test_unsupported_top_level_keyword_raises(self):
        schema = {
            "type": "object",
            "patternProperties": {"^S_": {"type": "string"}},
        }
        with pytest.raises(UnsupportedSchemaError, match="unsupported schema keyword 'patternProperties'"):
            assert_supported_schema(schema)

    def test_unsupported_string_constraint_keyword_raises(self):
        schema = {
            "type": "object",
            "properties": {
                "email": {"type": "string", "format": "email"},
            },
        }
        with pytest.raises(UnsupportedSchemaError, match="unsupported schema keyword 'format'"):
            assert_supported_schema(schema)

    def test_unsupported_additional_properties_false_raises(self):
        schema = {
            "type": "object",
            "additionalProperties": False,
        }
        with pytest.raises(UnsupportedSchemaError, match="'additionalProperties: False' is a constraint"):
            assert_supported_schema(schema)

    def test_unsupported_tuple_items_raises(self):
        schema = {
            "type": "object",
            "properties": {
                "coordinates": {
                    "type": "array",
                    "items": [{"type": "number"}, {"type": "number"}],
                },
            },
        }
        with pytest.raises(UnsupportedSchemaError, match="tuple-form 'items' .* is not supported"):
            assert_supported_schema(schema)


# ============================================================================
# 5. Full trace.schema.json Validation Tests
# ============================================================================

class TestTraceSchemaValidation:
    @pytest.fixture
    def trace_schema(self):
        return load_schema("trace.schema.json")

    @pytest.fixture
    def valid_trace(self):
        return {
            "id": "2026-09-20_sample-trace",
            "title": "Fix bug in tokenizer",
            "context_text": "Agent was tokenizing unicode strings with ascii regex.",
            "solution_text": "Switched to regex with re.UNICODE flag.",
            "tags": ["python", "nlp", "encoding"],
            "agent_type": "code",
            "agent_id": "worker-1",
            "profile": "code-review",
            "extensions": {"commit": "abc1234"},
            "outcome": {
                "resolved": True,
                "escalated": False,
                "repeated_error": False,
                "frustration_signal": False,
                "tokens_used": 1500,
                "llm_calls": 3,
                "baseline": False,
            },
            "shared_with_commons": False,
            "quarantined": False,
            "quarantine_reason": None,
        }

    def test_valid_trace_passes(self, trace_schema, valid_trace):
        errs = validate(valid_trace, trace_schema)
        assert errs == []

    @pytest.mark.parametrize(
        "missing_field",
        ["id", "title", "context_text", "solution_text", "tags", "agent_type"],
    )
    def test_missing_required_fields_fails(self, trace_schema, valid_trace, missing_field):
        del valid_trace[missing_field]
        errs = validate(valid_trace, trace_schema)
        assert any(f"missing required field '{missing_field}'" in e for e in errs)

    def test_empty_string_violates_minlength(self, trace_schema, valid_trace):
        valid_trace["title"] = ""
        errs = validate(valid_trace, trace_schema)
        assert any("'title': string shorter than minLength=1" in e for e in errs)

    def test_agent_id_exceeds_maxlength(self, trace_schema, valid_trace):
        valid_trace["agent_id"] = "a" * 129
        errs = validate(valid_trace, trace_schema)
        assert any("'agent_id': string longer than maxLength=128" in e for e in errs)

    def test_negative_outcome_tokens_used(self, trace_schema, valid_trace):
        valid_trace["outcome"]["tokens_used"] = -1
        errs = validate(valid_trace, trace_schema)
        assert any("tokens_used': -1 < minimum 0" in e for e in errs)

    def test_negative_outcome_llm_calls(self, trace_schema, valid_trace):
        valid_trace["outcome"]["llm_calls"] = -10
        errs = validate(valid_trace, trace_schema)
        assert any("llm_calls': -10 < minimum 0" in e for e in errs)

    def test_invalid_outcome_boolean_type(self, trace_schema, valid_trace):
        valid_trace["outcome"]["resolved"] = "yes"
        errs = validate(valid_trace, trace_schema)
        assert any("outcome.resolved': expected type ['boolean', 'null'], got str" in e for e in errs)

    def test_vote_enum_validation(self, trace_schema, valid_trace):
        valid_trace["votes"] = [
            {"vote_type": "invalid_vote"},
        ]
        errs = validate(valid_trace, trace_schema)
        assert any("vote_type" in e and "not in enum" in e for e in errs)

    def test_trust_range_validation(self, trace_schema, valid_trace):
        valid_trace["trust"] = 1.5
        errs = validate(valid_trace, trace_schema)
        assert any("'trust': 1.5 > maximum 1" in e for e in errs)

        valid_trace["trust"] = -0.1
        errs = validate(valid_trace, trace_schema)
        assert any("'trust': -0.1 < minimum 0" in e for e in errs)


# ============================================================================
# 6. Full lesson.schema.json Validation Tests
# ============================================================================

class TestLessonSchemaValidation:
    @pytest.fixture
    def lesson_schema(self):
        return load_schema("lesson.schema.json")

    @pytest.fixture
    def valid_lesson(self):
        return {
            "name": "lesson_safe_regex",
            "description": "Always use re.UNICODE for multilingual text.",
            "tags": ["regex", "nlp"],
            "agent_type": "code",
            "domain": "testing",
            "importance": 4,
            "importance_rationale": "Prevents data corruption on non-ASCII text.",
            "importance_history": [],
            "applies_when": "When writing regular expressions that parse text.",
            "do_not_apply_when": "When parsing strict ASCII protocols.",
            "uses": 2,
            "last_hit": "2026-09-18",
            "source_traces": ["trace_001"],
            "hub_trace_id": None,
            "status": "active",
        }

    def test_valid_lesson_passes(self, lesson_schema, valid_lesson):
        errs = validate(valid_lesson, lesson_schema)
        assert errs == []

    @pytest.mark.parametrize(
        "missing_field",
        [
            "name", "description", "tags", "agent_type", "domain",
            "importance", "importance_rationale", "applies_when",
            "do_not_apply_when", "uses", "last_hit", "status",
        ],
    )
    def test_missing_required_fields_fails(self, lesson_schema, valid_lesson, missing_field):
        del valid_lesson[missing_field]
        errs = validate(valid_lesson, lesson_schema)
        assert any(f"missing required field '{missing_field}'" in e for e in errs)

    def test_invalid_status_enum(self, lesson_schema, valid_lesson):
        for bad_status in ["deprecated", "draft", "deleted", "ACTIVE", "Active"]:
            valid_lesson["status"] = bad_status
            errs = validate(valid_lesson, lesson_schema)
            assert any(f"'status': {bad_status!r} not in enum ['active', 'review', 'archived']" in e for e in errs)

    def test_importance_bounds(self, lesson_schema, valid_lesson):
        valid_lesson["importance"] = 0
        errs = validate(valid_lesson, lesson_schema)
        assert any("'importance': 0 < minimum 1" in e for e in errs)

        valid_lesson["importance"] = 6
        errs = validate(valid_lesson, lesson_schema)
        assert any("'importance': 6 > maximum 5" in e for e in errs)

    def test_importance_type_mismatch(self, lesson_schema, valid_lesson):
        valid_lesson["importance"] = "4"
        errs = validate(valid_lesson, lesson_schema)
        assert any("'importance': expected type integer, got str" in e for e in errs)

        # bool not allowed as integer
        valid_lesson["importance"] = True
        errs = validate(valid_lesson, lesson_schema)
        assert any("'importance': expected type integer, got bool" in e for e in errs)

    def test_uses_negative(self, lesson_schema, valid_lesson):
        valid_lesson["uses"] = -1
        errs = validate(valid_lesson, lesson_schema)
        assert any("'uses': -1 < minimum 0" in e for e in errs)
