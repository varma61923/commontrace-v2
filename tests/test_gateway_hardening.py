import json

from commontrace import gateway
from commontrace.gateway import TransientAuthError, is_transient_auth_error


def _call(gw, method, path, body=None, headers=None):
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    resp = gw.handle(method, path, headers=headers or {}, body=payload)
    data = json.loads(resp.body.decode("utf-8")) if resp.body else None
    return resp.status, data, resp.headers


def test_transient_auth_error_classifier():
    # 5xx, timeouts, connection errors are transient
    assert is_transient_auth_error(TransientAuthError("gateway timeout", status=504))
    assert is_transient_auth_error(ConnectionResetError("connection reset"))
    assert is_transient_auth_error(TimeoutError("request timed out"))

    # Mock custom error with status code
    class CustomErr(Exception):
        status_code = 502

    assert is_transient_auth_error(CustomErr("bad gateway"))

    # 401 and 403 are permanent auth failures
    class BadCreds(Exception):
        status = 401

    assert not is_transient_auth_error(BadCreds("unauthorized"))

    class Forbidden(Exception):
        status = 403

    assert not is_transient_auth_error(Forbidden("forbidden"))


def test_gateway_capabilities_and_health(tmp_path):
    store = str(tmp_path / "store")
    gw = gateway.Gateway(store, token="secret-token-12345")

    # /v1/health has capabilities and tier
    status, health_data, _ = _call(gw, "GET", "/v1/health")
    assert status == 200
    assert health_data["ok"] is True
    assert "tier" in health_data
    assert "capabilities" in health_data

    # /v1/capabilities returns detailed matrix without requiring auth
    status, cap_data, _ = _call(gw, "GET", "/v1/capabilities")
    assert status == 200
    assert "tier" in cap_data
    caps = cap_data["capabilities"]
    assert caps["container_scoping"] is True
    assert caps["defense_screen"] is True
    assert caps["ssrf_guard"] is True


def test_gateway_whoami_and_container_tag(tmp_path):
    store = str(tmp_path / "store")
    gw = gateway.Gateway(store, token="secret-token-12345")

    # Unauthenticated whoami fails
    status, _, _ = _call(gw, "GET", "/v1/whoami")
    assert status == 401

    # Authenticated whoami with container tag
    auth_headers = {
        "Authorization": "Bearer secret-token-12345",
        "X-Container-Tag": "tenant-corp-42",
    }
    status, who_data, resp_headers = _call(gw, "GET", "/v1/whoami", headers=auth_headers)
    assert status == 200
    assert who_data["authenticated"] is True
    assert who_data["role"] == "admin"
    assert who_data["token_prefix"].startswith("secret-t...")
    assert who_data["container_tag"] == "tenant-corp-42"
    assert resp_headers.get("X-Container-Tag") == "tenant-corp-42"


def test_gateway_resolve_tag(tmp_path):
    store = str(tmp_path / "store")
    gw = gateway.Gateway(store, token="secret-token-12345")
    auth_headers = {"Authorization": "Bearer secret-token-12345"}

    # Valid tag
    status, res, _ = _call(
        gw, "POST", "/v1/resolve_tag", {"container_tag": "project-alpha_1"}, headers=auth_headers
    )
    assert status == 200
    assert res["valid"] is True
    assert res["container_tag"] == "project-alpha_1"
    assert res["scope"] == "container:project-alpha_1"

    # Invalid tag with disallowed characters
    status, err_res, _ = _call(
        gw, "POST", "/v1/resolve_tag", {"container_tag": "bad tag!@#"}, headers=auth_headers
    )
    assert status == 400
    assert err_res["error"]["code"] == "bad_request"
