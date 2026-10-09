"""The Hub's OpenAPI document covers every live route, and stays current.

No database: the Hub is built with every optional surface on and never
started (no lifespan), which registers every route without connecting.
"""
from __future__ import annotations

import json
import os

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route

from hub import auth
from hub.openapi import (
    DOCUMENT_PATH,
    OpenAPIDriftError,
    document,
    path_parameters,
    reference_app,
    render,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHECKED_IN = os.path.join(REPO_ROOT, "hub", "openapi.json")
METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")

# Routes that are deliberately reachable without credentials. Everything else
# must name at least one security scheme.
PUBLIC = {
    ("GET", "/healthz"), ("GET", "/readyz"), ("GET", "/disclosure"), ("GET", DOCUMENT_PATH),
    ("GET", "/scim/v2/ServiceProviderConfig"), ("GET", "/scim/v2/ResourceTypes"), ("GET", "/scim/v2/Schemas"),
    ("GET", "/signup"), ("POST", "/signup"), ("POST", "/api/v1/keys"),
    ("GET", "/app/signin"), ("POST", "/app/signin"), ("POST", "/app/signout"),
    ("GET", "/app/proof/shared/{token}"),
}


@pytest.fixture(scope="module")
def app():
    return reference_app()


@pytest.fixture(scope="module")
def doc():
    return document()


def _operations(doc):
    for path, item in doc["paths"].items():
        for method, operation in item.items():
            if method in METHODS:
                yield method.upper(), path, operation


def _live_routes(app):
    """(methods-or-None, path) for every route, walked independently of hub/openapi.py."""
    out = []
    for route in app.routes:
        if isinstance(route, Mount) and route.routes:
            out.extend((r.methods, route.path + r.path) for r in route.routes)
        else:
            out.append((getattr(route, "methods", None), route.path))
    return out


def test_every_live_route_and_method_is_documented(app, doc):
    live = _live_routes(app)
    assert len(live) > 90
    for methods, path in live:
        assert path in doc["paths"], f"{path} is served but not documented"
        documented = {m.upper() for m in doc["paths"][path] if m in METHODS}
        if methods is None:
            assert "POST" in documented, f"{path} accepts any method; at least POST must be documented"
            continue
        for method in methods - {"HEAD"}:
            assert method in documented, f"{method} {path} is served but not documented"


def test_head_duplicates_are_omitted(doc):
    assert not any(method == "HEAD" for method, _, _ in _operations(doc))


def test_the_document_route_and_mcp_transport_are_documented(doc):
    assert "get" in doc["paths"][DOCUMENT_PATH]
    assert {"get", "post", "delete"} <= set(doc["paths"]["/mcp"])
    names = {p["name"] for p in doc["paths"]["/mcp"]["post"]["parameters"]}
    assert "Mcp-Session-Id" in names


def test_operation_ids_are_unique(doc):
    ids = [operation["operationId"] for _, _, operation in _operations(doc)]
    assert len(ids) == len(set(ids)), sorted({i for i in ids if ids.count(i) > 1})


def test_every_path_parameter_is_declared(doc):
    for method, path, operation in _operations(doc):
        declared = {p["name"] for p in operation.get("parameters", []) if p["in"] == "path"}
        expected = set(path_parameters(path))
        assert declared == expected, f"{method} {path}: declares {declared}, template has {expected}"
        for parameter in operation.get("parameters", []):
            if parameter["in"] == "path":
                assert parameter["required"] is True


def test_every_operation_declares_security_and_secured_routes_name_a_scheme(doc):
    schemes = set(doc["components"]["securitySchemes"])
    for method, path, operation in _operations(doc):
        assert "security" in operation, f"{method} {path} declares no security"
        requirement = operation["security"]
        if (method, path) in PUBLIC:
            continue
        named = {name for alternative in requirement for name in alternative}
        assert named, f"{method} {path} is not in the public allowlist but declares no security scheme"
        assert named <= schemes, f"{method} {path} names unknown schemes {named - schemes}"


def test_every_operation_has_a_tag_summary_and_responses(doc):
    tags = {t["name"] for t in doc["tags"]}
    for method, path, operation in _operations(doc):
        assert operation["summary"], f"{method} {path}"
        assert set(operation["tags"]) <= tags, f"{method} {path}"
        assert any(code.startswith(("2", "3")) for code in operation["responses"]), f"{method} {path}"


def test_machine_api_json_bodies_are_typed(doc):
    """A machine API's JSON request/response names a schema with a type or a reference -- never `{}`."""
    machine_prefixes = ("/api/", "/v1/", "/scim/", "/connectors/", "/healthz", "/readyz", "/disclosure", "/mcp")
    for method, path, operation in _operations(doc):
        if not path.startswith(machine_prefixes):
            continue
        contents = [operation["requestBody"]["content"]] if "requestBody" in operation else []
        contents += [r["content"] for r in operation["responses"].values() if "content" in r]
        for content in contents:
            for media, entry in content.items():
                if "json" not in media:
                    continue
                schema = entry["schema"]
                assert "$ref" in schema or "type" in schema or "anyOf" in schema or "oneOf" in schema, (
                    f"{method} {path} {media}: untyped schema"
                )


def test_checked_in_document_is_current(doc):
    with open(CHECKED_IN, encoding="utf-8") as handle:
        checked_in = handle.read()
    assert checked_in == render(doc), "hub/openapi.json is stale; run python scripts/export_hub_openapi.py"


def test_document_is_a_valid_openapi_3_1_document(doc):
    validator = pytest.importorskip("openapi_spec_validator")
    assert doc["openapi"] == "3.1.0"
    validator.validate(doc)


def test_served_document_equals_the_generated_one(app, doc):
    from starlette.testclient import TestClient

    client = TestClient(app)
    response = client.get(DOCUMENT_PATH)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == json.loads(render(doc))
    again = client.get(DOCUMENT_PATH)
    assert again.content == response.content


def test_an_undocumented_route_refuses_to_build(app):
    async def secret(request):
        return PlainTextResponse("x")

    app.router.routes.append(Route("/api/v1/undocumented", secret, methods=["GET"]))
    try:
        with pytest.raises(OpenAPIDriftError, match="undocumented: GET /api/v1/undocumented"):
            document(app)
    finally:
        app.router.routes.pop()


def test_a_declaration_for_a_removed_route_refuses_to_build():
    async def healthz(request):
        return PlainTextResponse("ok")

    with pytest.raises(OpenAPIDriftError, match="declared but not served: GET /readyz"):
        document(Starlette(routes=[Route("/healthz", healthz, methods=["GET"])]))


def test_building_the_reference_app_leaves_live_auth_state_alone():
    saved = (auth._DEPLOYMENT_REGION, auth._AUTH_CACHE_TTL, auth._AUTH_CACHE_REQUIRES_LISTENER)
    try:
        auth.configure_region("eu-west")
        auth.configure_auth_cache(30, require_listener=False)
        reference_app()
        assert auth._DEPLOYMENT_REGION == "eu-west"
        assert auth._AUTH_CACHE_TTL == 30
        assert auth._AUTH_CACHE_REQUIRES_LISTENER is False
    finally:
        auth._DEPLOYMENT_REGION, auth._AUTH_CACHE_TTL, auth._AUTH_CACHE_REQUIRES_LISTENER = saved
