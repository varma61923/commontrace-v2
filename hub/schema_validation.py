"""Schema authority: protocol/schemas/*.json, loaded from disk at runtime."""

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
    _validate("trace.schema.json", obj)


def validate_lesson(obj: dict) -> None:
    _validate("lesson.schema.json", obj)
