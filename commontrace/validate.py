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

    path = os.path.join(schemas_dir(), name)
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


def validate(instance: dict, schema: dict) -> list[str]:
    """Return a list of human-readable error strings; empty list = valid."""
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
