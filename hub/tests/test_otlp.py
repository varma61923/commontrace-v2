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
        assert response.json() == {"accepted": 0, "skipped": 1, "occasions_resolved": 0, "errors": []}
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


class TestOccasionJoin:
    @staticmethod
    def _outcome_span(span_id, occasion, succeeded, key="commontrace.occasion_id"):
        return {"traceId": "t" * 32, "spanId": span_id, "name": "task finished", "status": {"code": "UNSET"},
                "attributes": [{"key": key, "value": {"stringValue": occasion}},
                               {"key": "commontrace.occasion.succeeded", "value": {"boolValue": succeeded}}]}

    async def _observation(self, session_factory, org_id, occasion):
        from hub import crud
        from hub.models import HoldoutObservation
        async with session_scope(session_factory) as session:
            org = await session.get(Organization, org_id)
            org.holdout_rate, org.holdout_salt = 0.5, "otlp-join"
            trace = Trace(org_id=org_id, title="t", context_text="c", solution_text="s", tags=[],
                          agent_type="support")
            session.add(trace)
            await session.flush()
            await crud.holdout_assign(session, org_id, [trace.id], occasion)
        return HoldoutObservation

    async def _succeeded(self, session_factory, model, org_id, occasion):
        async with session_scope(session_factory) as session:
            rows = (await session.execute(select(model.succeeded).where(
                model.org_id == org_id, model.occasion_id == occasion))).all()
        assert len(rows) == 1
        return rows[0][0]

    @pytest.mark.parametrize("key", ["commontrace.occasion_id", "session.id", "gen_ai.conversation.id"])
    async def test_an_explicit_outcome_closes_the_occasion_whichever_id_attribute_the_caller_emits(
            self, session_factory, config, key):
        org_id, raw_key = await _org_with_key(session_factory)
        model = await self._observation(session_factory, org_id, "ep-1")
        async with _client(_app(session_factory, config)) as client:
            r = await client.post(otlp.OTLP_TRACES_PATH, headers=_key(raw_key),
                                  json=_otlp_body(self._outcome_span("b" * 16, "ep-1", True, key)))
        assert r.status_code == 200 and r.json()["occasions_resolved"] == 1
        assert await self._succeeded(session_factory, model, org_id, "ep-1") is True

    async def test_a_status_alone_never_decides_the_occasion(self, session_factory, config):
        org_id, raw_key = await _org_with_key(session_factory)
        model = await self._observation(session_factory, org_id, "ep-2")
        span = _span("c" * 16, status_code="OK")
        span["attributes"].append({"key": "commontrace.occasion_id", "value": {"stringValue": "ep-2"}})
        async with _client(_app(session_factory, config)) as client:
            r = await client.post(otlp.OTLP_TRACES_PATH, json=_otlp_body(span), headers=_key(raw_key))
        assert r.json()["occasions_resolved"] == 0
        assert await self._succeeded(session_factory, model, org_id, "ep-2") is None

    async def test_the_first_report_wins_so_an_exporter_retry_cannot_flip_it(self, session_factory, config):
        org_id, raw_key = await _org_with_key(session_factory)
        model = await self._observation(session_factory, org_id, "ep-3")
        async with _client(_app(session_factory, config)) as client:
            await client.post(otlp.OTLP_TRACES_PATH, headers=_key(raw_key),
                              json=_otlp_body(self._outcome_span("d" * 16, "ep-3", False)))
            r = await client.post(otlp.OTLP_TRACES_PATH, headers=_key(raw_key),
                                  json=_otlp_body(self._outcome_span("e" * 16, "ep-3", True)))
        assert r.json()["occasions_resolved"] == 0
        assert await self._succeeded(session_factory, model, org_id, "ep-3") is False

    async def test_another_orgs_occasion_is_untouched(self, session_factory, config):
        org_a, _ = await _org_with_key(session_factory, "A")
        org_b, key_b = await _org_with_key(session_factory, "B")
        model = await self._observation(session_factory, org_a, "ep-4")
        async with _client(_app(session_factory, config)) as client:
            r = await client.post(otlp.OTLP_TRACES_PATH, headers=_key(key_b),
                                  json=_otlp_body(self._outcome_span("f" * 16, "ep-4", True)))
        assert r.json()["occasions_resolved"] == 0
        assert await self._succeeded(session_factory, model, org_a, "ep-4") is None
