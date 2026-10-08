"""Prescribed entity properties: full optional JSON Schema, conservative core fallback."""
from __future__ import annotations

from commontrace.ontology import OntologyError


def validate_properties(value: dict, schema: dict) -> None:
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        from commontrace import validate

        def strip_annotations(spec):
            if not isinstance(spec, dict):
                return spec
            return {k: ({name: strip_annotations(s) for name, s in v.items()} if k == "properties"
                        else strip_annotations(v) if k == "items" else v)
                    for k, v in spec.items() if k not in ("$defs", "$schema", "title", "description", "default")}
        simple = strip_annotations(schema)
        extra = simple.pop("additionalProperties", True)
        try:
            validate.assert_supported_schema(simple)
        except validate.UnsupportedSchemaError as exc:
            raise OntologyError("this property schema requires commontrace[ontology]: " + str(exc)) from exc
        errors = validate.validate(value, simple)
        if extra is False and set(value) - set(simple.get("properties", {})):
            errors.append("entity has undeclared properties")
        elif extra is not True and extra is not False:
            raise OntologyError("schema-valued additionalProperties requires commontrace[ontology]")
    else:
        try:
            Draft202012Validator.check_schema(schema)
            errors = [e.message for e in Draft202012Validator(schema).iter_errors(value)]
        except Exception as exc:
            raise OntologyError("invalid prescribed entity property schema") from exc
    if errors:
        raise OntologyError("invalid entity properties: " + "; ".join(errors))
