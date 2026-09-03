"""Schema authority: protocol/schemas/*.json, loaded from disk at runtime.

Per the brief this server implements: "Validate every inbound and outbound
object against protocol/schemas/trace.schema.json and lesson.schema.json
loaded from disk at runtime. Do not hand-write duplicate validation logic and
do not copy the schema into the server." Both schema files are loaded here,
generically, by path -- neither is transcribed into Python.

Only `validate_trace` is exercised by the current tool set (see hub/models.py
for why there is no Lesson table / Lesson-shaped tool). `validate_lesson` is
provided for completeness and for any future Hub tool that trades in Lesson
objects, so that path doesn't require new validation plumbing.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

import jsonschema

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCHEMAS_DIR = _REPO_ROOT / "protocol" / "schemas"


class SchemaValidationError(ValueError):
    """Raised when an object fails validation against a protocol schema."""

    def __init__(self, schema_name: str, errors: list[str]):
        self.schema_name = schema_name
        self.errors = errors
        super().__init__(f"{schema_name}: {'; '.join(errors)}")


_ALLOWED_SCHEMA_FILENAMES = frozenset({"trace.schema.json", "lesson.schema.json"})


@cache
def _load_schema(filename: str) -> dict:
    # Every current caller passes one of the two literals in
    # _ALLOWED_SCHEMA_FILENAMES (validate_trace/validate_lesson below) --
    # `filename` is never derived from a request or any other untrusted
    # input today. Checked anyway, before it ever reaches a filesystem
    # path: a future caller passing something else through would otherwise
    # have no signal that this function was never meant to resolve an
    # arbitrary name, matching how every other read path in this Hub
    # enforces a boundary rather than relying on the absence of a caller
    # that could cross it.
    if filename not in _ALLOWED_SCHEMA_FILENAMES:
        raise ValueError(f"unrecognized protocol schema name: {filename!r}")
    path = _SCHEMAS_DIR / filename
    if not path.is_file():
        raise FileNotFoundError(
            f"Protocol schema {filename!r} not found at {path}. The Hub must be run "
            "from within (or alongside) a commontrace-v2 checkout that has protocol/schemas/."
        )
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


@cache
def _validator(filename: str) -> jsonschema.protocols.Validator:
    schema = _load_schema(filename)
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    return validator_cls(schema)


def _validate(filename: str, obj: dict) -> None:
    validator = _validator(filename)
    errors = sorted(validator.iter_errors(obj), key=lambda e: list(e.path))
    if errors:
        messages = [f"{'.'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors]
        raise SchemaValidationError(filename, messages)


def validate_trace(obj: dict) -> None:
    """Raise SchemaValidationError if `obj` does not conform to
    protocol/schemas/trace.schema.json."""
    _validate("trace.schema.json", obj)


def validate_lesson(obj: dict) -> None:
    """Raise SchemaValidationError if `obj` does not conform to
    protocol/schemas/lesson.schema.json. Not currently called by any Hub
    tool -- see hub/models.py module docstring."""
    _validate("lesson.schema.json", obj)
