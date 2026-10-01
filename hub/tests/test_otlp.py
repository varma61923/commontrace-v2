"""Tests for hub/otlp.py -- `POST /v1/traces`, the OTLP/HTTP ingest path.

The property that matters most: every span is normalized through
commontrace/adapters.py's `normalize(span, source="otel")`, the SAME
function `commontrace import --source otel` and
commontrace/otel_exporter.py both already call -- so this file does not
re-test that parsing (tests/test_adapters.py and
tests/test_otel_exporter.py already do), it tests that THIS path wires it
to `crud.contribute_trace` correctly: auth, scopes, tenant isolation,
idempotent replay, and the explicit protobuf refusal.
"""
from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select
from starlette.applications import Starlette

from hub import auth, otlp
from hub.abuse import make_rate_limiter
from hub.db import session_scope
from hub.models import Organization, Trace

pytestmark = pytest.mark.asyncio


def _app(session_factory, config, *, enabled: bool = True) -> Starlette:
    app = Starlette()
    if enabled:
        otlp.add_otlp_routes(
            app, session_factory, config=config, rate_limiter=make_rate_limiter(config),
        )
    return app


def _client(app: Starlette) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _org_with_key(session_factory, name="Acme", scopes=None):
    async with session_scope(session_factory) as session:
        org = Organization(name=name)
        session.add(org)
        await session.flush()
        issued = await auth.issue_api_key(session, org.id, scopes=scopes)
        return org.id, issued.raw_key


def _key(raw_key: str) -> dict[str, str]:
    return {"X-API-Key": raw_key}


def _span(span_id: str, *, prompt: str = "handle the refund", completion: str = "issued a refund",
          status_code: str = "OK") -> dict:
    """One OTLP-JSON span, GenAI semantic conventions, current draft."""
    return {
        "traceId": "t" * 32,
        "spanId": span_id,
        "name": "chat completion",
        "status": {"code": status_code},
        "attributes": [
            {"key": "gen_ai.prompt", "value": {"stringValue": prompt}},
            {"key": "gen_ai.completion", "value": {"stringValue": completion}},
            {"key": "gen_ai.system", "value": {"stringValue": "openai"}},
        ],
    }


def _otlp_body(*spans: dict) -> dict:
    return {"resourceSpans": [{"resource": {}, "scopeSpans": [{"scope": {}, "spans": list(spans)}]}]}


class TestAbsentUnlessEnabled:
    async def test_no_route_when_not_mounted(self, session_factory, config):
        async with _client(_app(session_factory, config, enabled=False)) as client:
            response = await client.post(otlp.OTLP_TRACES_PATH, json=_otlp_body(_span("a" * 16)))
        assert response.status_code == 404


class TestAuthentication:
    async def test_missing_api_key_is_401(self, session_factory, config):
        async with _client(_app(session_factory, config)) as client:
            response = await client.post(otlp.OTLP_TRACES_PATH, json=_otlp_body(_span("a" * 16)))
        assert response.status_code == 401

    async def test_an_invalid_key_is_401(self, session_factory, config):
        async with _client(_app(session_factory, config)) as client:
            response = await client.post(
                otlp.OTLP_TRACES_PATH, json=_otlp_body(_span("a" * 16)),
                headers=_key("ct_not_a_real_key"),
            )
        assert response.status_code == 401

    async def test_a_read_only_key_is_403(self, session_factory, config):
        _org_id, raw_key = await _org_with_key(session_factory, scopes="read")
        async with _client(_app(session_factory, config)) as client:
            response = await client.post(
                otlp.OTLP_TRACES_PATH, json=_otlp_body(_span("a" * 16)), headers=_key(raw_key),
            )
        assert response.status_code == 403


class TestIngest:
    async def test_a_well_formed_span_becomes_a_trace(self, session_factory, config):
        org_id, raw_key = await _org_with_key(session_factory)
        async with _client(_app(session_factory, config)) as client:
            response = await client.post(
                otlp.OTLP_TRACES_PATH, json=_otlp_body(_span("a" * 16)), headers=_key(raw_key),
            )
        assert response.status_code == 200
        body = response.json()
        assert body["accepted"] == 1
        assert body["skipped"] == 0
        assert body["errors"] == []

        async with session_scope(session_factory) as session:
            rows = (await session.execute(select(Trace).where(Trace.org_id == org_id))).scalars().all()
        assert len(rows) == 1
        assert rows[0].profile == "otel"
        assert "issued a refund" in rows[0].solution_text

    async def test_multiple_spans_become_multiple_traces(self, session_factory, config):
        _org_id, raw_key = await _org_with_key(session_factory)
        body = _otlp_body(_span("a" * 16), _span("b" * 16, prompt="different issue"))
        async with _client(_app(session_factory, config)) as client:
            response = await client.post(otlp.OTLP_TRACES_PATH, json=body, headers=_key(raw_key))
        assert response.json()["accepted"] == 2

    async def test_an_error_status_span_records_a_negative_outcome(self, session_factory, config):
        org_id, raw_key = await _org_with_key(session_factory)
        body = _otlp_body(_span("a" * 16, status_code="ERROR"))
        async with _client(_app(session_factory, config)) as client:
            await client.post(otlp.OTLP_TRACES_PATH, json=body, headers=_key(raw_key))
        async with session_scope(session_factory) as session:
            rows = (await session.execute(select(Trace).where(Trace.org_id == org_id))).scalars().all()
        assert rows[0].outcome.get("resolved") is False

    async def test_a_span_with_no_content_is_skipped_not_stored(self, session_factory, config):
        org_id, raw_key = await _org_with_key(session_factory)
        empty_span = {"traceId": "t" * 32, "spanId": "a" * 16, "name": "noop", "attributes": []}
        async with _client(_app(session_factory, config)) as client:
            response = await client.post(
                otlp.OTLP_TRACES_PATH, json=_otlp_body(empty_span), headers=_key(raw_key),
            )
        assert response.json() == {"accepted": 0, "skipped": 1, "errors": []}
        async with session_scope(session_factory) as session:
            rows = (await session.execute(select(Trace).where(Trace.org_id == org_id))).scalars().all()
        assert rows == []

    async def test_resending_the_same_span_id_does_not_duplicate_the_trace(self, session_factory, config):
        org_id, raw_key = await _org_with_key(session_factory)
        body = _otlp_body(_span("a" * 16))
        async with _client(_app(session_factory, config)) as client:
            first = await client.post(otlp.OTLP_TRACES_PATH, json=body, headers=_key(raw_key))
            second = await client.post(otlp.OTLP_TRACES_PATH, json=body, headers=_key(raw_key))
        assert first.json()["accepted"] == 1
        assert second.json()["accepted"] == 1
        assert second.json()["errors"] == []
        async with session_scope(session_factory) as session:
            rows = (await session.execute(select(Trace).where(Trace.org_id == org_id))).scalars().all()
        assert len(rows) == 1

    async def test_traces_are_scoped_to_the_authenticated_org(self, session_factory, config):
        org_a, key_a = await _org_with_key(session_factory, name="A")
        org_b, key_b = await _org_with_key(session_factory, name="B")
        async with _client(_app(session_factory, config)) as client:
            await client.post(otlp.OTLP_TRACES_PATH, json=_otlp_body(_span("a" * 16)), headers=_key(key_a))
            await client.post(otlp.OTLP_TRACES_PATH, json=_otlp_body(_span("b" * 16)), headers=_key(key_b))
        async with session_scope(session_factory) as session:
            rows_a = (await session.execute(select(Trace).where(Trace.org_id == org_a))).scalars().all()
            rows_b = (await session.execute(select(Trace).where(Trace.org_id == org_b))).scalars().all()
        assert len(rows_a) == 1 and len(rows_b) == 1
        assert rows_a[0].id != rows_b[0].id


class TestBodyValidation:
    async def test_protobuf_content_type_is_refused_explicitly(self, session_factory, config):
        _org_id, raw_key = await _org_with_key(session_factory)
        async with _client(_app(session_factory, config)) as client:
            response = await client.post(
                otlp.OTLP_TRACES_PATH, content=b"\x00\x01\x02",
                headers={**_key(raw_key), "content-type": "application/x-protobuf"},
            )
        assert response.status_code == 415

    async def test_malformed_json_is_400(self, session_factory, config):
        _org_id, raw_key = await _org_with_key(session_factory)
        async with _client(_app(session_factory, config)) as client:
            response = await client.post(
                otlp.OTLP_TRACES_PATH, content=b"{not json",
                headers={**_key(raw_key), "content-type": "application/json"},
            )
        assert response.status_code == 400

    async def test_a_non_object_body_is_400(self, session_factory, config):
        _org_id, raw_key = await _org_with_key(session_factory)
        async with _client(_app(session_factory, config)) as client:
            response = await client.post(otlp.OTLP_TRACES_PATH, json=[1, 2, 3], headers=_key(raw_key))
        assert response.status_code == 400

    async def test_empty_resource_spans_is_a_clean_no_op(self, session_factory, config):
        _org_id, raw_key = await _org_with_key(session_factory)
        async with _client(_app(session_factory, config)) as client:
            response = await client.post(
                otlp.OTLP_TRACES_PATH, json={"resourceSpans": []}, headers=_key(raw_key),
            )
        assert response.status_code == 200
        assert response.json() == {"accepted": 0, "skipped": 0, "errors": []}

    async def test_over_the_span_count_limit_is_413(self, session_factory, config, monkeypatch):
        _org_id, raw_key = await _org_with_key(session_factory)
        monkeypatch.setattr(otlp, "_MAX_SPANS_PER_REQUEST", 2)
        body = _otlp_body(_span("a" * 16), _span("b" * 16), _span("c" * 16))
        async with _client(_app(session_factory, config)) as client:
            response = await client.post(otlp.OTLP_TRACES_PATH, json=body, headers=_key(raw_key))
        assert response.status_code == 413
