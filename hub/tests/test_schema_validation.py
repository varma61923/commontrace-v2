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
