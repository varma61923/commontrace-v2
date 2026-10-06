from __future__ import annotations

import json
import socket
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from hub import events
from hub.db import session_scope
from hub.encryption import EnvelopeCipher, generate_key
from hub.models import Organization, WebhookDelivery, WebhookEndpoint

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
KEY = "test-deployment-signing-key"
URL = "https://example.invalid/hooks/commontrace"


async def _fake_public_resolve(hostname: str) -> list:
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0))]


def _fake_resolve_to(ip: str):
    async def resolve(hostname: str) -> list:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0))]
    return resolve


@pytest.fixture(autouse=True)
def _skip_real_dns_for_webhook_hosts(monkeypatch):
    monkeypatch.setattr(events, "_default_resolve", _fake_public_resolve)


@pytest_asyncio.fixture
async def org(session_factory):
    async with session_scope(session_factory) as session:
        o = Organization(name="fleet")
        session.add(o)
        await session.flush()
        return o.id


@pytest_asyncio.fixture
async def endpoint(session_factory, org):
    async with session_scope(session_factory) as session:
        ep, secret = await events.add_endpoint(session, org, URL, signing_key=KEY)
        await session.flush()
        return {"id": ep.id, "secret": secret, "org": org}


class Recorder:
    def __init__(self, fail_times: int = 0, error="ConnectionError"):
        self.calls: list[tuple[str, str, dict]] = []
        self.fail_times = fail_times
        self.error = error

    async def __call__(self, url: str, body: str, headers: dict) -> None:
        self.calls.append((url, body, headers))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError(self.error)


@pytest.mark.asyncio
class TestPayloadWhitelist:
    async def test_a_declared_field_is_accepted(self):
        assert events.check_payload(
            "trace.created", {"trace_id": "t1", "agent_type": "support"}
        ) == {"trace_id": "t1", "agent_type": "support"}

    async def test_trace_content_is_refused_by_name(self, session_factory, org):
        with pytest.raises(events.EventError) as exc:
            events.check_payload(
                "trace.created",
                {"trace_id": "t1", "context_text": "the customer's private data"},
            )
        assert "context_text" in str(exc.value)
        assert "never trace content" in str(exc.value)

    async def test_any_undeclared_field_is_refused_not_just_known_bad_ones(self):
        with pytest.raises(events.EventError, match="notes"):
            events.check_payload("trace.created", {"notes": "harmless"})

    async def test_a_nested_object_is_refused(self):
        with pytest.raises(events.EventError, match="nested object"):
            events.check_payload("trace.created", {"trace_id": {"inner": "x"}})

    async def test_a_prose_length_string_is_refused(self):
        with pytest.raises(events.EventError, match="prose"):
            events.check_payload("trace.quarantined", {"reason": "x" * 500})

    async def test_an_unknown_event_type_lists_the_real_ones(self):
        with pytest.raises(events.EventError) as exc:
            events.check_payload("trace.exploded", {})
        assert "experiment.verdict" in str(exc.value)

    async def test_every_event_type_declares_fields_and_a_description(self):
        for name, spec in events.EVENT_TYPES.items():
            assert spec.describe, name
            assert spec.fields, name

    async def test_no_event_type_declares_a_content_field(self):
        forbidden = {
            "context_text", "solution_text", "body", "text", "content",
            "input", "output", "prompt", "completion", "title",
        }
        for name, spec in events.EVENT_TYPES.items():
            assert not (set(spec.fields) & forbidden), name


class TestSigning:
    def test_a_signature_verifies(self):
        body = '{"a":1}'
        header = events.signature_header("s3cret", int(NOW.timestamp()), body)
        assert events.verify_signature(
            "s3cret", header, body, now=int(NOW.timestamp()))

    def test_a_tampered_body_fails(self):
        body = '{"a":1}'
        header = events.signature_header("s3cret", int(NOW.timestamp()), body)
        assert not events.verify_signature(
            "s3cret", header, '{"a":2}', now=int(NOW.timestamp()))

    def test_a_wrong_secret_fails(self):
        body = '{"a":1}'
        header = events.signature_header("s3cret", int(NOW.timestamp()), body)
        assert not events.verify_signature(
            "other", header, body, now=int(NOW.timestamp()))

    def test_an_old_delivery_is_rejected_as_a_replay(self):
        body = '{"a":1}'
        stamp = int(NOW.timestamp())
        header = events.signature_header("s3cret", stamp, body)
        later = stamp + events.REPLAY_TOLERANCE_SECONDS + 1
        assert not events.verify_signature("s3cret", header, body, now=later)

    def test_moving_the_timestamp_breaks_the_signature(self):
        body = '{"a":1}'
        stamp = int(NOW.timestamp())
        header = events.signature_header("s3cret", stamp, body)
        forged = header.replace(f"t={stamp}", f"t={stamp + 600}")
        assert not events.verify_signature(
            "s3cret", forged, body, now=stamp + 600)

    def test_a_malformed_header_is_false_not_a_crash(self):
        for header in ("", "garbage", "t=notanumber,v1=abc", "v1=abc"):
            assert not events.verify_signature("s", header, "{}", now=1)

    def test_the_secret_is_derived_not_stored(self):
        a = events.derive_secret(KEY, "endpoint-1", 1)
        assert a == events.derive_secret(KEY, "endpoint-1", 1)
        assert a != events.derive_secret(KEY, "endpoint-2", 1)
        assert a != events.derive_secret(KEY, "endpoint-1", 2)
        assert a != events.derive_secret("other-key", "endpoint-1", 1)

    def test_no_signing_key_is_a_clear_refusal(self):
        with pytest.raises(events.EventError, match="HUB_LEDGER_SIGNING_KEY"):
            events.derive_secret("", "endpoint-1", 1)


@pytest.mark.asyncio
class TestEndpoints:
    async def test_plaintext_http_is_refused(self, session_factory, org):
        async with session_scope(session_factory) as session:
            with pytest.raises(events.EventError, match="https"):
                await events.add_endpoint(
                    session, org, "http://example.invalid/hook", signing_key=KEY)

    async def test_an_unknown_subscribed_event_is_refused(self, session_factory, org):
        async with session_scope(session_factory) as session:
            with pytest.raises(events.EventError, match="unknown event type"):
                await events.add_endpoint(
                    session, org, URL, events=["trace.exploded"], signing_key=KEY)

    async def test_subscribing_to_nothing_means_everything(self, session_factory, org):
        async with session_scope(session_factory) as session:
            endpoint, _ = await events.add_endpoint(session, org, URL, signing_key=KEY)
        assert set(endpoint.events) == set(events.EVENT_NAMES)

    async def test_the_endpoint_row_holds_no_secret(self, session_factory, endpoint):
        async with session_scope(session_factory) as session:
            row = await session.get(WebhookEndpoint, endpoint["id"])
            columns = {c.name for c in row.__table__.columns}
        assert not (columns & {"secret", "signing_secret", "secret_hash", "token"})
        values = " ".join(str(getattr(row, c)) for c in columns)
        assert endpoint["secret"] not in values

    async def test_rotating_changes_the_secret(self, session_factory, endpoint):
        async with session_scope(session_factory) as session:
            rotated = await events.rotate_secret(
                session, endpoint["id"], signing_key=KEY)
        assert rotated != endpoint["secret"]
        body = "{}"
        header = events.signature_header(rotated, int(NOW.timestamp()), body)
        assert not events.verify_signature(
            endpoint["secret"], header, body, now=int(NOW.timestamp()))


@pytest.mark.asyncio
class TestAtRestEncryption:
    async def test_no_cipher_stores_url_as_plaintext(self, session_factory, org):
        async with session_scope(session_factory) as session:
            endpoint, _ = await events.add_endpoint(session, org, URL, signing_key=KEY)
        assert endpoint.url == URL

    async def test_a_configured_cipher_stores_ciphertext_not_the_url(
        self, session_factory, org
    ):
        cipher = EnvelopeCipher.from_config(generate_key(), "")
        async with session_scope(session_factory) as session:
            endpoint, _ = await events.add_endpoint(
                session, org, URL, signing_key=KEY, cipher=cipher)
        async with session_scope(session_factory) as session:
            row = await session.get(WebhookEndpoint, endpoint.id)
        assert row.url != URL
        assert URL not in row.url
        assert cipher.decrypt(row.url) == URL

    async def test_ssrf_validation_still_runs_on_the_plaintext_url(
        self, session_factory, org, monkeypatch
    ):
        monkeypatch.setattr(
            events, "_default_resolve", _fake_resolve_to("127.0.0.1"))
        cipher = EnvelopeCipher.from_config(generate_key(), "")
        async with session_scope(session_factory) as session:
            with pytest.raises(events.EventError, match="private"):
                await events.add_endpoint(
                    session, org, URL, signing_key=KEY, cipher=cipher)

    async def test_delivery_decrypts_the_url_before_transporting(
        self, session_factory, org
    ):
        cipher = EnvelopeCipher.from_config(generate_key(), "")
        async with session_scope(session_factory) as session:
            await events.add_endpoint(session, org, URL, signing_key=KEY, cipher=cipher)
            await events.emit(session, org, "trace.created", {"trace_id": "t1"}, now=NOW)

        transport = Recorder()
        async with session_scope(session_factory) as session:
            result = await events.deliver_pending(
                session, transport, signing_key=KEY, now=NOW, cipher=cipher)
        assert result.delivered == 1
        assert transport.calls[0][0] == URL

    async def test_delivery_with_the_wrong_cipher_fails_closed(
        self, session_factory, org
    ):
        cipher = EnvelopeCipher.from_config(generate_key(), "")
        async with session_scope(session_factory) as session:
            await events.add_endpoint(session, org, URL, signing_key=KEY, cipher=cipher)
            await events.emit(session, org, "trace.created", {"trace_id": "t1"}, now=NOW)

        wrong_cipher = EnvelopeCipher.from_config(generate_key(), "")
        transport = Recorder()
        async with session_scope(session_factory) as session:
            result = await events.deliver_pending(
                session, transport, signing_key=KEY, now=NOW, cipher=wrong_cipher)
        assert result.delivered == 0
        assert result.retrying == 1
        assert transport.calls == []


@pytest.mark.asyncio
class TestSsrfProtection:
    @pytest.mark.parametrize("ip", [
        "127.0.0.1",
        "10.0.0.5",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "224.0.0.1",
        "0.0.0.0",
        "::1",
        "::ffff:127.0.0.1",
        "100.100.100.200",
        "100.64.0.1",
    ])
    async def test_a_private_or_internal_target_is_rejected(self, ip):
        with pytest.raises(events.EventError, match="private"):
            await events._reject_private_target(
                URL, resolve=_fake_resolve_to(ip))

    async def test_a_genuinely_public_target_is_accepted(self):
        await events._reject_private_target(URL, resolve=_fake_resolve_to("8.8.8.8"))
        await events._reject_private_target(URL, resolve=_fake_resolve_to("2606:4700::1111"))

    async def test_an_unresolvable_host_is_refused_not_silently_allowed(self):
        async def resolve(hostname):
            raise socket.gaierror("nodename nor servname provided")
        with pytest.raises(events.EventError, match="could not resolve"):
            await events._reject_private_target(URL, resolve=resolve)

    async def test_a_url_with_no_host_is_refused(self):
        with pytest.raises(events.EventError, match="resolvable host"):
            await events._reject_private_target("https:///no-host-here")

    async def test_add_endpoint_refuses_a_url_resolving_to_a_private_address(
        self, session_factory, org, monkeypatch
    ):
        monkeypatch.setattr(
            events, "_default_resolve", _fake_resolve_to("127.0.0.1"))
        async with session_scope(session_factory) as session:
            with pytest.raises(events.EventError, match="private"):
                await events.add_endpoint(session, org, URL, signing_key=KEY)

    async def test_add_endpoint_accepts_a_url_resolving_publicly(
        self, session_factory, org, monkeypatch
    ):
        monkeypatch.setattr(
            events, "_default_resolve", _fake_resolve_to("8.8.8.8"))
        async with session_scope(session_factory) as session:
            endpoint, _ = await events.add_endpoint(session, org, URL, signing_key=KEY)
        assert endpoint.url == URL

    async def test_http_transport_rechecks_at_delivery_time_not_only_registration(
        self, session_factory, org, monkeypatch
    ):
        calls = {"n": 0}

        async def rebinding_resolve(hostname):
            calls["n"] += 1
            ip = "8.8.8.8" if calls["n"] == 1 else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0))]

        monkeypatch.setattr(events, "_default_resolve", rebinding_resolve)
        async with session_scope(session_factory) as session:
            await events.add_endpoint(session, org, URL, signing_key=KEY)
            await events.emit(session, org, "trace.created", {"trace_id": "t1"}, now=NOW)

        transport = events.http_transport()
        async with session_scope(session_factory) as session:
            result = await events.deliver_pending(
                session, transport, signing_key=KEY, now=NOW)
        assert result.delivered == 0
        assert result.retrying == 1


@pytest.mark.asyncio
class TestEmitting:
    async def test_an_org_with_no_endpoints_queues_nothing_and_does_not_raise(
        self, session_factory, org
    ):
        async with session_scope(session_factory) as session:
            queued = await events.emit(
                session, org, "trace.created", {"trace_id": "t1"})
        assert queued == []

    async def test_emitting_queues_one_delivery(self, session_factory, endpoint):
        async with session_scope(session_factory) as session:
            await events.emit(
                session, endpoint["org"], "trace.created", {"trace_id": "t1"})
        async with session_scope(session_factory) as session:
            rows = list((await session.execute(select(WebhookDelivery))).scalars())
        assert len(rows) == 1
        assert rows[0].event_type == "trace.created"
        assert rows[0].payload == {"trace_id": "t1"}
        assert rows[0].status == events.STATUS_PENDING

    async def test_an_unsubscribed_event_is_not_queued(self, session_factory, org):
        async with session_scope(session_factory) as session:
            await events.add_endpoint(
                session, org, URL, events=["trace.quarantined"], signing_key=KEY)
        async with session_scope(session_factory) as session:
            await events.emit(session, org, "trace.created", {"trace_id": "t1"})
        async with session_scope(session_factory) as session:
            assert await session.scalar(
                select(func.count()).select_from(WebhookDelivery)) == 0

    async def test_a_disabled_endpoint_is_not_queued_to(self, session_factory, endpoint):
        async with session_scope(session_factory) as session:
            row = await session.get(WebhookEndpoint, endpoint["id"])
            row.enabled = False
        async with session_scope(session_factory) as session:
            await events.emit(
                session, endpoint["org"], "trace.created", {"trace_id": "t1"})
        async with session_scope(session_factory) as session:
            assert await session.scalar(
                select(func.count()).select_from(WebhookDelivery)) == 0

    async def test_another_orgs_endpoint_never_receives_this_orgs_events(
        self, session_factory, endpoint
    ):
        async with session_scope(session_factory) as session:
            other = Organization(name="someone else")
            session.add(other)
            await session.flush()
            other_id = other.id
        async with session_scope(session_factory) as session:
            await events.emit(session, other_id, "trace.created", {"trace_id": "t1"})
        async with session_scope(session_factory) as session:
            assert await session.scalar(
                select(func.count()).select_from(WebhookDelivery)) == 0

    async def test_a_bad_payload_raises_before_anything_is_queued(
        self, session_factory, endpoint
    ):
        async with session_scope(session_factory) as session:
            with pytest.raises(events.EventError):
                await events.emit(
                    session, endpoint["org"], "trace.created",
                    {"context_text": "secret"},
                )
        async with session_scope(session_factory) as session:
            assert await session.scalar(
                select(func.count()).select_from(WebhookDelivery)) == 0


@pytest.mark.asyncio
class TestDelivery:
    async def _queue(self, session_factory, org, n=1):
        async with session_scope(session_factory) as session:
            for i in range(n):
                await events.emit(
                    session, org, "trace.created", {"trace_id": f"t{i}"}, now=NOW)

    async def test_a_delivery_is_signed_with_the_endpoints_secret(
        self, session_factory, endpoint
    ):
        await self._queue(session_factory, endpoint["org"])
        transport = Recorder()
        async with session_scope(session_factory) as session:
            result = await events.deliver_pending(
                session, transport, signing_key=KEY, now=NOW)
        assert result.delivered == 1
        url, body, headers = transport.calls[0]
        assert url == URL
        assert events.verify_signature(
            endpoint["secret"], headers["X-CommonTrace-Signature"], body,
            now=int(NOW.timestamp()),
        )

    async def test_the_envelope_carries_a_stable_idempotency_key(
        self, session_factory, endpoint
    ):
        await self._queue(session_factory, endpoint["org"])
        first = Recorder(fail_times=1)
        async with session_scope(session_factory) as session:
            await events.deliver_pending(session, first, signing_key=KEY, now=NOW)
        later = NOW + timedelta(hours=1)
        second = Recorder()
        async with session_scope(session_factory) as session:
            await events.deliver_pending(session, second, signing_key=KEY, now=later)

        first_body = json.loads(first.calls[0][1])
        second_body = json.loads(second.calls[0][1])
        assert second_body["event_id"] == first_body["event_id"]
        assert first_body["attempt"] == 1
        assert second_body["attempt"] == 2

    async def test_the_envelope_carries_no_content(self, session_factory, endpoint):
        await self._queue(session_factory, endpoint["org"])
        transport = Recorder()
        async with session_scope(session_factory) as session:
            await events.deliver_pending(session, transport, signing_key=KEY, now=NOW)
        body = json.loads(transport.calls[0][1])
        assert set(body) == {
            "event_id", "type", "org_id", "created_at", "attempt", "data"}
        assert set(body["data"]) <= set(events.EVENT_TYPES["trace.created"].fields)

    async def test_a_failure_is_retried_later_not_immediately(
        self, session_factory, endpoint
    ):
        await self._queue(session_factory, endpoint["org"])
        transport = Recorder(fail_times=5)
        async with session_scope(session_factory) as session:
            result = await events.deliver_pending(
                session, transport, signing_key=KEY, now=NOW)
        assert result.retrying == 1 and result.delivered == 0

        async with session_scope(session_factory) as session:
            again = await events.deliver_pending(
                session, transport, signing_key=KEY, now=NOW)
        assert again.attempted == 0

    async def test_the_error_is_recorded_for_the_operator(
        self, session_factory, endpoint
    ):
        await self._queue(session_factory, endpoint["org"])
        async with session_scope(session_factory) as session:
            await events.deliver_pending(
                session, Recorder(fail_times=1, error="host unreachable https://user:secret@hooks.example/?token=private"),
                signing_key=KEY, now=NOW,
            )
        async with session_scope(session_factory) as session:
            row = (await session.execute(select(WebhookDelivery))).scalar_one()
        assert row.last_error == "RuntimeError"

    async def test_it_gives_up_loudly_rather_than_retrying_forever(
        self, session_factory, endpoint
    ):
        await self._queue(session_factory, endpoint["org"])
        transport = Recorder(fail_times=99)
        moment = NOW
        for _ in range(events.MAX_ATTEMPTS):
            async with session_scope(session_factory) as session:
                await events.deliver_pending(
                    session, transport, signing_key=KEY, now=moment)
            moment += timedelta(days=1)

        async with session_scope(session_factory) as session:
            row = (await session.execute(select(WebhookDelivery))).scalar_one()
            failed = await events.failed_deliveries(session, endpoint["org"])
        assert row.status == events.STATUS_FAILED
        assert row.attempts == events.MAX_ATTEMPTS
        assert len(failed) == 1

    async def test_a_success_after_a_failure_clears_the_error(
        self, session_factory, endpoint
    ):
        await self._queue(session_factory, endpoint["org"])
        transport = Recorder(fail_times=1)
        async with session_scope(session_factory) as session:
            await events.deliver_pending(session, transport, signing_key=KEY, now=NOW)
        later = NOW + timedelta(hours=1)
        async with session_scope(session_factory) as session:
            result = await events.deliver_pending(
                session, transport, signing_key=KEY, now=later)
        assert result.delivered == 1
        async with session_scope(session_factory) as session:
            row = (await session.execute(select(WebhookDelivery))).scalar_one()
        assert row.status == events.STATUS_DELIVERED
        assert row.last_error == ""
        assert row.delivered_at is not None

    async def test_an_endpoint_deleted_after_queueing_is_given_up_with_a_reason(
        self, session_factory, endpoint
    ):
        await self._queue(session_factory, endpoint["org"])
        async with session_scope(session_factory) as session:
            row = await session.get(WebhookEndpoint, endpoint["id"])
            row.enabled = False
        transport = Recorder()
        async with session_scope(session_factory) as session:
            result = await events.deliver_pending(
                session, transport, signing_key=KEY, now=NOW)
        assert result.gave_up == 1
        assert transport.calls == []
        async with session_scope(session_factory) as session:
            delivery = (await session.execute(select(WebhookDelivery))).scalar_one()
        assert "disabled" in delivery.last_error

    async def test_a_delivered_event_is_not_delivered_again(
        self, session_factory, endpoint
    ):
        await self._queue(session_factory, endpoint["org"])
        transport = Recorder()
        async with session_scope(session_factory) as session:
            await events.deliver_pending(session, transport, signing_key=KEY, now=NOW)
        async with session_scope(session_factory) as session:
            again = await events.deliver_pending(
                session, transport, signing_key=KEY, now=NOW + timedelta(days=1))
        assert again.attempted == 0
        assert len(transport.calls) == 1

    async def test_pending_count_tracks_the_queue(self, session_factory, endpoint):
        await self._queue(session_factory, endpoint["org"], n=3)
        async with session_scope(session_factory) as session:
            assert await events.pending_count(session, endpoint["org"]) == 3
        async with session_scope(session_factory) as session:
            await events.deliver_pending(
                session, Recorder(), signing_key=KEY, now=NOW)
        async with session_scope(session_factory) as session:
            assert await events.pending_count(session, endpoint["org"]) == 0


@pytest.mark.asyncio
class TestEmittedFromRealPaths:
    async def test_a_legal_hold_announces_itself(self, session_factory, endpoint):
        from hub import retention

        async with session_scope(session_factory) as session:
            await retention.place_hold(
                session, endpoint["org"], reason="Ohio subpoena 2026-44",
                placed_by="ops",
            )
        async with session_scope(session_factory) as session:
            row = (await session.execute(select(WebhookDelivery))).scalar_one()
        assert row.event_type == "legal_hold.placed"
        assert "Ohio" not in json.dumps(row.payload)

    async def test_a_purge_announces_counts_and_the_plan_digest(
        self, session_factory, endpoint
    ):
        from hub import retention

        async with session_scope(session_factory) as session:
            await retention.set_policy(session, endpoint["org"], "trace", 90)
        async with session_scope(session_factory) as session:
            plan = await retention.plan(session, endpoint["org"])
            await retention.apply(session, endpoint["org"], plan.digest)
        async with session_scope(session_factory) as session:
            rows = list((await session.execute(
                select(WebhookDelivery).where(
                    WebhookDelivery.event_type == "retention.purged")
            )).scalars())
        assert len(rows) == 1
        assert set(rows[0].payload) == {"plan", "n_deleted", "n_held"}
