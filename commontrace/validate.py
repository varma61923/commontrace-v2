"""Minimal JSON Schema (draft 2020-12 subset) validator.

Deliberately small: covers exactly what protocol/schemas/*.json use
(type, required, enum, minLength, minimum/maximum, items) so `commontrace`
doesn't pull in a full `jsonschema` dependency just to validate two schemas.
Not a general-purpose validator — do not extend the schemas beyond this
subset without extending this file too.
"""
from __future__ import annotations

import json
import os
from typing import Any

_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
    "null": type(None),
}


def load_schema(name: str) -> dict:
    """Load a bundled schema by file name, e.g. 'trace.schema.json'."""
    from commontrace.paths import schemas_dir

    # os.path.basename only treats "/" as a separator on POSIX, so a name
    # containing "\" or ":" (Windows separators/drive letters) would pass
    # `basename(name) == name` unchanged here even though it is not a bare
    # filename. Rejecting those characters explicitly keeps this check
    # platform-independent rather than relying on the host OS's path rules.
    if (
        not isinstance(name, str)
        or os.path.basename(name) != name
        or any(sep in name for sep in ("\\", ":"))
        or not (name.endswith(".schema.json") or name.endswith(".json"))
    ):
        raise ValueError(f"Invalid or unsafe schema file name: {name!r}")

    base_dir = os.path.abspath(schemas_dir())
    path = os.path.abspath(os.path.join(base_dir, name))
    if not (path == base_dir or path.startswith(base_dir + os.sep)):
        raise ValueError(f"Path traversal detected in schema name: {name!r}")

    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _check_type(value: Any, expected: str | list) -> bool:
    expected_types = expected if isinstance(expected, list) else [expected]
    for et in expected_types:
        py_type = _TYPE_MAP.get(et)
        if py_type is None:
            continue
        # bool is an int subclass in Python; neither JSON Schema "integer" nor
        # "number" should accept a boolean, or minimum/maximum silently never run.
        if et in ("integer", "number") and isinstance(value, bool):
            continue
        if isinstance(value, py_type):
            return True
    return False


# Every keyword `_validate_value` and `validate` actually act on, plus the
# purely descriptive ones that carry no constraint. Anything outside this set
# is silently ignored by this validator -- so a schema edit adding `pattern`,
# `format`, or `maxLength` would report OK while enforcing nothing.
# `assert_supported_schema` turns that silent no-op into a loud failure at
# exactly the moment someone widens a schema.
_SUPPORTED_KEYWORDS = frozenset({
    # structural
    "type", "properties", "required", "items",
    # constraints this file implements
    "enum", "minLength", "minimum", "maximum",
    # descriptive only -- no runtime effect, safe to ignore
    "$schema", "$id", "title", "description", "default", "examples",
})

# Keywords whose only permissive value is a no-op. `additionalProperties: true`
# means "anything else is fine", which is exactly what ignoring it does; any
# other value would be a real constraint this validator cannot enforce.
_PERMISSIVE_ONLY = {"additionalProperties": (True,)}


class UnsupportedSchemaError(ValueError):
    """A schema uses a keyword this minimal validator does not implement.

    Raised rather than ignored because the failure mode is invisible: the
    validator would accept any value at all for the constrained field while
    reporting the document valid.
    """


def assert_supported_schema(schema: dict, path: str = "<root>") -> None:
    """Raise if `schema` uses a keyword this validator does not enforce."""
    for key, value in schema.items():
        if key in _PERMISSIVE_ONLY:
            if value not in _PERMISSIVE_ONLY[key]:
                raise UnsupportedSchemaError(
                    f"{path}: '{key}: {value!r}' is a constraint this validator "
                    f"does not enforce (only {_PERMISSIVE_ONLY[key]} is a no-op)"
                )
            continue
        if key not in _SUPPORTED_KEYWORDS:
            raise UnsupportedSchemaError(
                f"{path}: unsupported schema keyword {key!r}. commontrace/validate.py "
                "implements a deliberate subset; add support there before using it, "
                "or the constraint will be silently unenforced."
            )

    for name, sub in (schema.get("properties") or {}).items():
        if isinstance(sub, dict):
            assert_supported_schema(sub, f"{path}.{name}")
    items = schema.get("items")
    if isinstance(items, dict):
        assert_supported_schema(items, f"{path}[]")


def validate(instance: dict, schema: dict) -> list[str]:
    """Return a list of human-readable error strings; empty list = valid."""
    if not isinstance(instance, dict):
        return ["Instance must be a dictionary / JSON object"]

    errors: list[str] = []
    properties = schema.get("properties", {})
    required = schema.get("required", [])

    for field in required:
        if field not in instance:
            errors.append(f"missing required field '{field}'")

    for key, value in instance.items():
        prop_schema = properties.get(key)
        if prop_schema is None:
            continue
        errors.extend(_validate_value(f"{key}", value, prop_schema))

    return errors


def _validate_value(label: str, value: Any, schema: dict) -> list[str]:
    errors: list[str] = []
    if value is None and schema.get("type") not in (None, "null") and "null" not in (
        schema.get("type") if isinstance(schema.get("type"), list) else [schema.get("type")]
    ):
        # Allow None only if the field's type explicitly includes "null"
        if schema.get("type") is not None:
            errors.append(f"'{label}': null not allowed")
        return errors

    expected_type = schema.get("type")
    if expected_type and not _check_type(value, expected_type):
        errors.append(f"'{label}': expected type {expected_type}, got {type(value).__name__}")
        return errors  # further checks assume the right type

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"'{label}': {value!r} not in enum {schema['enum']}")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"'{label}': string shorter than minLength={schema['minLength']}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"'{label}': {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"'{label}': {value} > maximum {schema['maximum']}")

    if isinstance(value, list) and "items" in schema:
        item_schema = schema["items"]
        for i, item in enumerate(value):
            errors.extend(_validate_value(f"{label}[{i}]", item, item_schema))

    if isinstance(value, dict) and "properties" in schema:
        nested_properties = schema["properties"]
        nested_required = schema.get("required", [])
        for field in nested_required:
            if field not in value:
                errors.append(f"'{label}': missing required field '{field}'")
        for key, item in value.items():
            item_schema = nested_properties.get(key)
            if item_schema is None:
                continue
            errors.extend(_validate_value(f"{label}.{key}", item, item_schema))

    return errors
