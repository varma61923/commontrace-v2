"""Machine-readable requests for the identical local and hosted memory API."""
from __future__ import annotations

OCCASION = {"type": ["string", "null"], "maxLength": 256}
TEXT = {"type": "string", "maxLength": 20000}
CONTEXT = {"type": "array", "items": {"type": "string", "maxLength": 256}, "maxItems": 100}
FIELDS = {
    "add": ({"text": {**TEXT, "minLength": 1}, "local": {"type": "boolean"}, "memory_type": TEXT}, ["text"]),
    "batch": ({"items": {"type": "array", "items": {"type": "object"}, "maxItems": 200}}, ["items"]),
    "search": ({"query": TEXT, "recipe": TEXT, "retriever": TEXT, "center": TEXT,
                "as_of": {"type": ["string", "null"]}, "action_class": TEXT,
                "limit": {"type": "integer", "minimum": 0, "maximum": 1000}}, ["query"]),
    "profile": ({"query": TEXT, "action_class": TEXT, "occasion_id": OCCASION,
                 "limit": {"type": "integer", "minimum": 0, "maximum": 1000}}, []),
    "reflect": ({"query": TEXT, "action_class": TEXT, "occasion_id": OCCASION,
                 "budget": {"type": "integer", "minimum": 0, "maximum": 100000},
                 "exploration_slots": {"type": "integer", "minimum": 0, "maximum": 100}}, ["query"]),
    "outcome": ({"occasion_id": {"type": "string", "minLength": 1, "maxLength": 256},
                 "succeeded": {"type": "boolean"}}, ["occasion_id", "succeeded"]),
    "check-action": ({"tool": {**TEXT, "minLength": 1}, "tags": CONTEXT}, ["tool"]),
    "propose": ({"text": {**TEXT, "minLength": 1}, "sources": {**CONTEXT, "minItems": 1}}, ["text", "sources"]),
}


def request_schema(operation: str) -> dict:
    fields, required = FIELDS[operation]
    return {"type": "object", "properties": {**fields, "context": CONTEXT},
            "required": required, "additionalProperties": False}


def validate_request(operation: str, body: dict) -> None:
    """Enforce this contract's bounded structural subset without an extra dependency."""
    def check(value, schema, path):
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        kind = ("null" if value is None else "boolean" if isinstance(value, bool) else
                "string" if isinstance(value, str) else "integer" if isinstance(value, int) else
                "array" if isinstance(value, list) else "object" if isinstance(value, dict) else "invalid")
        if kind not in types:
            raise ValueError(path+" has the wrong type")
        if kind == "string" and not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 20000):
            raise ValueError(path+" exceeds its text bounds")
        if kind == "integer" and not schema.get("minimum", value) <= value <= schema.get("maximum", value):
            raise ValueError(path+" exceeds its integer bounds")
        if kind == "array":
            if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 1000):
                raise ValueError(path+" exceeds its item bounds")
            for index, item in enumerate(value):
                check(item, schema["items"], path+"["+str(index)+"]")
        if kind == "object":
            fields = schema.get("properties", {})
            if not set(schema.get("required", [])) <= value.keys():
                raise ValueError(path+" is missing required fields")
            if schema.get("additionalProperties") is False and set(value)-fields.keys():
                raise ValueError(path+" contains undocumented fields")
            for key in value.keys() & fields.keys():
                check(value[key], fields[key], path+"."+key)
    check(body, request_schema(operation), "request")


def openapi_schema(schema: dict) -> dict:
    """Translate nullable JSON Schema types into OpenAPI 3.0 nullable syntax."""
    value = {key: openapi_schema(item) if isinstance(item, dict) else
             [openapi_schema(v) if isinstance(v, dict) else v for v in item] if isinstance(item, list) else item
             for key, item in schema.items()}
    if isinstance(value.get("type"), list):
        kinds = [kind for kind in value["type"] if kind != "null"]
        if len(kinds) != 1 or "null" not in value["type"]:
            raise ValueError("OpenAPI conversion requires a single nullable type")
        value.update(type=kinds[0], nullable=True)
    return value


def model_name(operation: str) -> str:
    return "".join(word.title() for word in operation.split("-"))


def components() -> dict:
    """Typed request/response contracts consumed by standard Swagger generators."""
    string_array = {"type": "array", "items": {"type": "string"}}
    obj = {"type": "object", "additionalProperties": True}
    evidence = {"type": "object", "properties": {"id": {"type": "string"}, "text": {"type": "string"},
        "score": {"type": "number"}, "layer": {"type": "string"}, "source_traces": string_array,
        "sources": string_array},
        "required": ["id", "text"], "additionalProperties": True}
    fact = {"type": "object", "properties": {"id": {"type": "string"}, "statement": TEXT,
        "action": {"type": "string", "enum": ["ADD", "NOOP"]}, "scopes": string_array,
        "source_traces": string_array, "origin": obj}, "required": ["id", "statement", "action"],
        "additionalProperties": True}
    control = {"type": "object", "properties": {"id": {"type": "string"}, "kind": {"type": "string"},
        "text": TEXT, "revision": {"type": "string"}, "scopes": string_array, "data": obj, "origin": obj},
        "required": ["id", "kind", "text", "revision"], "additionalProperties": True}
    def array(name):
        return {"type": "array", "items": {"$ref": "#/components/schemas/"+name}}
    responses = {
        "add": {"calls": {"type": "integer"}, "add_only": {"type": "boolean"}, "facts": array("Fact"),
                "entity_ids": string_array},
        "batch": {"add_only": {"type": "boolean"}, "facts": array("Fact")},
        "search": {"results": array("Evidence")},
        "profile": {"static": array("Evidence"), "dynamic": array("Evidence"),
                    "directives": array("ControlRecord"), "occasion_id": {"type": "string"},
                    "recent_activity": {"type": "array", "items": {"type": "object", "properties": {
                        "id": {"type": "string"}, "activity_type": {"type": "string"},
                        "text": {"type": "string"}, "recorded_at": {"type": "string"}},
                        "required": ["id", "activity_type", "text", "recorded_at"]}},
                    "withheld": string_array},
        "reflect": {"context": {"type": "string"}, "tokens_estimate": {"type": "integer"},
                    "budget": {"type": "integer"}, "evidence": array("Evidence"),
                    "occasion_id": {"type": "string"}, "withheld": string_array,
                    "withdrawn": {"type": "object", "additionalProperties": obj},
                    "exploration": {"type": "array", "items": obj}, "authority_sources": string_array},
        "outcome": {"recorded": {"type": "boolean"}},
        "check-action": {"allowed": {"type": "boolean"}},
    }
    schemas = {"Evidence": evidence, "Fact": fact, "ControlRecord": control,
               "ErrorResponse": {"type": "object", "properties": {"error": {"type": "object", "properties": {
                   "code": {"type": "string"}, "message": {"type": "string"}}, "required": ["code", "message"]}}}}
    for operation in FIELDS:
        name = model_name(operation)
        schemas[name+"Request"] = openapi_schema(request_schema(operation))
        if operation == "propose":
            schemas[name+"Response"] = {"allOf": [{"$ref": "#/components/schemas/ControlRecord"}]}
        else:
            properties = responses[operation]
            schemas[name+"Response"] = {"type": "object", "properties": properties,
                "required": list(properties), "additionalProperties": True}
    return schemas
