from __future__ import annotations

import pytest

from hub.schema_validation import SchemaValidationError, validate_lesson, validate_trace


def test_valid_trace_passes():
    validate_trace(
        {
            "id": "t1",
            "title": "Something",
            "context_text": "context",
            "solution_text": "solution",
            "tags": ["a"],
            "agent_type": "code",
        }
    )


def test_missing_required_field_rejected():
    with pytest.raises(SchemaValidationError):
        validate_trace({"id": "t1", "title": "Something", "context_text": "c", "tags": [], "agent_type": "code"})


def test_empty_required_string_rejected():
    with pytest.raises(SchemaValidationError):
        validate_trace(
            {
                "id": "t1",
                "title": "",
                "context_text": "c",
                "solution_text": "s",
                "tags": [],
                "agent_type": "code",
            }
        )


def test_lesson_schema_also_loads_from_disk():
    # Not exercised by any Hub tool today (see hub/models.py), but the
    # loader itself must work generically for both schema files.
    with pytest.raises(SchemaValidationError):
        validate_lesson({})


class TestLoadSchemaRejectsAnUnrecognizedFilename:
    """[SEC-HUB-02]: no caller today passes anything but the two literal
    schema filenames (validate_trace/validate_lesson above are the only
    callers), so this is defense in depth against a future one rather than
    a fix for a reachable path today -- consistent with how every other
    boundary in this Hub is enforced rather than left to rest on the
    absence of a caller that could cross it."""

    def test_a_path_outside_the_schemas_dir_is_rejected(self):
        from hub.schema_validation import _load_schema

        with pytest.raises(ValueError, match="unrecognized"):
            _load_schema("../../../../etc/passwd")

    def test_an_unrecognized_but_locally_named_file_is_also_rejected(self):
        from hub.schema_validation import _load_schema

        with pytest.raises(ValueError, match="unrecognized"):
            _load_schema("not_a_real_schema.json")
