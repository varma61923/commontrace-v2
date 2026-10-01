"""Outcome connectors end to end, against Postgres: signed deliveries from the
vendors' own example payloads become recorded outcomes, and nothing else does.

What these pin, in the order a reviewer would ask:
  * authentic or nothing: bad signature, stale timestamp, unknown/disabled
    connector, wrong tenant's secret -- all 401, all changing nothing, and the
    unknown and bad-signature answers are byte-identical;
  * replay: the same delivery twice, and eight copies at once, apply exactly once;
  * windows: a solved ticket is a pending candidate, matures to success only after
    its window, and a reopen inside the window is a failure at once;
  * dry-run: nothing recorded, what-it-would-do is, and going live processes a
    delivery first seen in dry-run;
  * tenancy: the same occasion id in two orgs never crosses.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import func, select, update
from starlette.applications import Starlette

from hub import crud
from hub.config import HubConfig
from hub.connector_routes import add_connector_routes
from hub.connectors import service
from hub.db import session_scope
from hub.encryption import EnvelopeCipher, generate_key
from hub.models import (
    AuditLogEntry,
    Connector,
    ConnectorDelivery,
    HoldoutObservation,
    Organization,
    PendingOutcome,
    Trace,
)

pytestmark = pytest.mark.asyncio

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "connectors")
ZD_SECRET = "dGhpc19zZWNyZXRfaXNfZm9yX3Rlc3Rpbmdfb25seQ=="
GH_SECRET = "It's a Secret to Everybody"
CIPHER = EnvelopeCipher.from_config(generate_key(), "")


def _fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


def _stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _zd(payload: dict, *, secret=ZD_SECRET, at: datetime | None = None):
    body = json.dumps(payload).encode()
    stamp = _stamp(at or datetime.now(timezone.utc))
    sig = base64.b64encode(hmac.new(secret.encode(), stamp.encode() + body, hashlib.sha256).digest()).decode()
    return {"X-Zendesk-Webhook-Signature": sig, "X-Zendesk-Webhook-Signature-Timestamp": stamp}, body


def _zd_status(current, previous, *, ticket="1244", event_id="evt-1"):
    payload = _fixture("zendesk_ticket_status_changed.json")
    payload["detail"]["id"] = ticket
    payload["event"]["current"], payload["event"]["previous"] = current, previous
    payload["id"] = event_id
    # The vendor example is dated 2025; a live event carries the current time, and
    # an old one would (correctly) mature immediately.
    payload["time"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return payload


def _gh(payload: dict, event: str, delivery: str, secret=GH_SECRET):
    body = json.dumps(payload).encode()
    return {
        "X-Hub-Signature-256": "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(),
        "X-GitHub-Event": event, "X-GitHub-Delivery": delivery,
    }, body


async def _org(session_factory, name="Acme"):
    async with session_scope(session_factory) as session:
        # A running experiment, so holdout_assign will record observations.
        org = Organization(name=name, holdout_rate=0.5, holdout_salt="connector-tests")
        session.add(org)
        await session.flush()
        return org.id


async def _connector(session_factory, org_id, provider="zendesk", secret=ZD_SECRET, *,
                     config=None, live=True):
    async with session_scope(session_factory) as session:
        connector = await service.create_connector(
            session, org_id, provider, secret, cipher=CIPHER, config=config or {})
        if live:
            await service.set_live(session, connector.id, True)
        return connector.id


async def _occasion(session_factory, org_id, occasion):
    """An unresolved observation for `occasion`, made the way a real fleet makes one."""
    async with session_scope(session_factory) as session:
        trace = Trace(org_id=org_id, title="t", context_text="c", solution_text="s", tags=[], agent_type="support")
        session.add(trace)
        await session.flush()
        await crud.holdout_assign(session, org_id, [trace.id], occasion)


async def _outcome(session_factory, org_id, occasion):
    async with session_scope(session_factory) as session:
        rows = (await session.execute(
            select(HoldoutObservation.succeeded)
            .where(HoldoutObservation.org_id == org_id, HoldoutObservation.occasion_id == occasion)
        )).all()
    assert len(rows) == 1, rows
    return rows[0][0]


async def _count(session_factory, model, **where):
    async with session_scope(session_factory) as session:
        query = select(func.count()).select_from(model)
        for column, value in where.items():
            query = query.where(getattr(model, column) == value)
        return (await session.execute(query)).scalar_one()


async def _post(session_factory, connector_id, headers, body, **kw):
    return await service.ingest(session_factory, connector_id, headers, body, cipher=CIPHER, **kw)


# --- Authenticity -----------------------------------------------------------------


class TestAuthenticOrNothing:
    async def test_a_bad_signature_changes_nothing(self, session_factory):
        org = await _org(session_factory)
        cid = await _connector(session_factory, org, config={"window_days": 0})
        await _occasion(session_factory, org, "1244")
        headers, body = _zd(_zd_status("SOLVED", "OPEN"), secret="not-the-secret")
        result = await _post(session_factory, cid, headers, body)
        assert result.status == 401
        assert await _outcome(session_factory, org, "1244") is None
        assert await _count(session_factory, ConnectorDelivery) == 0

    async def test_unknown_disabled_and_malformed_ids_look_exactly_like_a_bad_signature(self, session_factory):
        org = await _org(session_factory)
        live = await _connector(session_factory, org)
        off = await _connector(session_factory, org)
        async with session_scope(session_factory) as session:
            await service.set_enabled(session, off, False)
        headers, body = _zd(_zd_status("SOLVED", "OPEN"))
        bad_headers, _ = _zd(_zd_status("SOLVED", "OPEN"), secret="x")
        answers = {
            (await _post(session_factory, c, h, body)).status.__str__()
            + json.dumps((await _post(session_factory, c, h, body)).body, sort_keys=True)
            for c, h in [
                (live, bad_headers), ("00000000-0000-0000-0000-000000000000", headers),
                (off, headers), ("not-a-uuid", headers), ("' OR 1=1", headers),
            ]
        }
        assert len(answers) == 1, answers
        assert answers.pop().startswith("401")

    async def test_a_validly_signed_but_stale_delivery_is_a_replay(self, session_factory):
        org = await _org(session_factory)
        cid = await _connector(session_factory, org, config={"window_days": 0})
        await _occasion(session_factory, org, "1244")
        old = datetime.now(timezone.utc) - timedelta(minutes=10)
        headers, body = _zd(_zd_status("SOLVED", "OPEN"), at=old)
        assert (await _post(session_factory, cid, headers, body)).status == 401
        assert await _outcome(session_factory, org, "1244") is None

    async def test_another_tenants_secret_does_not_authenticate_against_this_connector(self, session_factory):
        a, b = await _org(session_factory, "A"), await _org(session_factory, "B")
        a_conn = await _connector(session_factory, a, secret="secret-of-a")
        await _connector(session_factory, b, secret="secret-of-b")
        headers, body = _zd(_zd_status("SOLVED", "OPEN"), secret="secret-of-b")
        assert (await _post(session_factory, a_conn, headers, body)).status == 401

    async def test_a_sealed_secret_copied_to_another_org_does_not_open(self, session_factory):
        a, b = await _org(session_factory, "A"), await _org(session_factory, "B")
        a_conn = await _connector(session_factory, a)
        b_conn = await _connector(session_factory, b, secret="b-own")
        async with session_scope(session_factory) as session:
            sealed = (await session.get(Connector, a_conn)).secret
            (await session.get(Connector, b_conn)).secret = sealed
        headers, body = _zd(_zd_status("SOLVED", "OPEN"))  # signed with A's secret
        assert (await _post(session_factory, b_conn, headers, body)).status == 401

    async def test_a_signed_body_that_is_not_json_is_a_400_not_a_500(self, session_factory):
        org = await _org(session_factory)
        cid = await _connector(session_factory, org)
        for body in (b"not json", b"[1,2]", b"\xff\xfe"):
            stamp = _stamp(datetime.now(timezone.utc))
            digest = hmac.new(ZD_SECRET.encode(), stamp.encode() + body, hashlib.sha256).digest()
            sig = base64.b64encode(digest).decode()
            result = await _post(session_factory, cid, {
                "X-Zendesk-Webhook-Signature": sig, "X-Zendesk-Webhook-Signature-Timestamp": stamp}, body)
            assert result.status == 400, body

    async def test_an_authentic_but_unmappable_payload_is_acknowledged_not_retried(self, session_factory):
        org = await _org(session_factory)
        cid = await _connector(session_factory, org)
        payload = _zd_status("SOLVED", "OPEN")
        payload["detail"]["id"] = None
        headers, body = _zd(payload)
        result = await _post(session_factory, cid, headers, body)
        assert result.status == 200 and result.body["status"] == "unusable"


# --- Replay and idempotency ---------------------------------------------------------


class TestReplay:
    async def test_the_same_delivery_twice_applies_once(self, session_factory):
        org = await _org(session_factory)
        cid = await _connector(session_factory, org, config={"window_days": 0})
        await _occasion(session_factory, org, "1244")
        headers, body = _zd(_zd_status("SOLVED", "OPEN"))
        first = await _post(session_factory, cid, headers, body)
        second = await _post(session_factory, cid, headers, body)
        assert first.body["status"] == "ok" and second.body["status"] == "duplicate"
        assert await _outcome(session_factory, org, "1244") is True
        assert await _count(session_factory, ConnectorDelivery) == 1

    async def test_eight_simultaneous_copies_apply_once_and_none_errors(self, session_factory):
        org = await _org(session_factory)
        cid = await _connector(session_factory, org, config={"window_days": 0})
        await _occasion(session_factory, org, "1244")
        headers, body = _zd(_zd_status("SOLVED", "OPEN"))
        results = await asyncio.gather(*[_post(session_factory, cid, headers, body) for _ in range(8)])
        assert [r.status for r in results] == [200] * 8
        assert sorted(r.body["status"] for r in results).count("ok") == 1
        assert await _count(session_factory, ConnectorDelivery) == 1
        assert await _outcome(session_factory, org, "1244") is True

    async def test_a_late_event_never_flips_an_outcome_already_counted(self, session_factory):
        org = await _org(session_factory)
        cid = await _connector(session_factory, org, config={"window_days": 0})
        await _occasion(session_factory, org, "1244")
        await _post(session_factory, cid, *_zd(_zd_status("SOLVED", "OPEN", event_id="e1")))
        bad = _fixture("zendesk_ticket_csat_received.json")
        bad["detail"]["id"], bad["event"]["satisfaction_score"]["score"], bad["id"] = "1244", "BAD", "e2"
        await _post(session_factory, cid, *_zd(bad))
        # record_occasion_outcome fills unresolved rows only, so the late BAD cannot flip it.
        assert await _outcome(session_factory, org, "1244") is True


# --- Windows ------------------------------------------------------------------------


class TestWindows:
    async def test_solved_waits_out_its_window_and_then_succeeds(self, session_factory):
        org = await _org(session_factory)
        cid = await _connector(session_factory, org, config={"window_days": 7})
        await _occasion(session_factory, org, "1244")
        result = await _post(session_factory, cid, *_zd(_zd_status("SOLVED", "OPEN")))
        assert result.body["signals"][0]["kind"] == "candidate_success"
        assert await _outcome(session_factory, org, "1244") is None        # not yet
        assert await _count(session_factory, PendingOutcome) == 1

        now = datetime.now(timezone.utc)
        assert await service.finalize_matured(session_factory, now=now + timedelta(days=6)) == 0
        assert await _outcome(session_factory, org, "1244") is None
        assert await service.finalize_matured(session_factory, now=now + timedelta(days=8)) == 1
        assert await _outcome(session_factory, org, "1244") is True
        assert await _count(session_factory, PendingOutcome) == 0
        assert await service.finalize_matured(session_factory, now=now + timedelta(days=9)) == 0

    async def test_a_reopen_inside_the_window_is_a_failure_at_once(self, session_factory):
        org = await _org(session_factory)
        cid = await _connector(session_factory, org, config={"window_days": 7})
        await _occasion(session_factory, org, "1244")
        await _post(session_factory, cid, *_zd(_zd_status("SOLVED", "OPEN", event_id="e1")))
        await _post(session_factory, cid, *_zd(_zd_status("OPEN", "SOLVED", event_id="e2")))
        assert await _outcome(session_factory, org, "1244") is False
        assert await _count(session_factory, PendingOutcome) == 0
        later = datetime.now(timezone.utc) + timedelta(days=30)
        assert await service.finalize_matured(session_factory, now=later) == 0
        assert await _outcome(session_factory, org, "1244") is False

    async def test_a_reopen_after_the_window_does_not_count_against_it(self, session_factory):
        org = await _org(session_factory)
        cid = await _connector(session_factory, org, config={"window_days": 7})
        await _occasion(session_factory, org, "1244")
        await _post(session_factory, cid, *_zd(_zd_status("SOLVED", "OPEN", event_id="e1")))
        await service.finalize_matured(session_factory, now=datetime.now(timezone.utc) + timedelta(days=8))
        result = await _post(session_factory, cid, *_zd(_zd_status("OPEN", "SOLVED", event_id="e2")))
        assert "nothing pending" in result.body["signals"][0]["action"]
        assert await _outcome(session_factory, org, "1244") is True

    async def test_a_bad_csat_fails_a_pending_candidate_immediately(self, session_factory):
        org = await _org(session_factory)
        cid = await _connector(session_factory, org, config={"window_days": 7})
        await _occasion(session_factory, org, "74184")
        solved = _zd_status("SOLVED", "OPEN", ticket="74184", event_id="e1")
        await _post(session_factory, cid, *_zd(solved))
        bad = _fixture("zendesk_ticket_csat_received.json")
        bad["event"]["satisfaction_score"]["score"], bad["id"] = "BAD", "e2"
        await _post(session_factory, cid, *_zd(bad))
        assert await _outcome(session_factory, org, "74184") is False
        assert await _count(session_factory, PendingOutcome) == 0

    async def test_a_vendor_timestamp_from_the_far_future_cannot_postpone_maturity_forever(self, session_factory):
        org = await _org(session_factory)
        cid = await _connector(session_factory, org, config={"window_days": 7})
        payload = _zd_status("SOLVED", "OPEN")
        payload["time"] = "2099-01-01T00:00:00Z"
        await _post(session_factory, cid, *_zd(payload))
        async with session_scope(session_factory) as session:
            mature = (await session.execute(select(PendingOutcome.mature_at))).scalar_one()
        assert mature < datetime.now(timezone.utc) + timedelta(days=8)

    async def test_the_occasion_prefix_joins_to_what_the_agent_used(self, session_factory):
        org = await _org(session_factory)
        cid = await _connector(session_factory, org, config={"window_days": 0, "occasion_prefix": "ZD-"})
        await _occasion(session_factory, org, "ZD-1244")
        await _post(session_factory, cid, *_zd(_zd_status("SOLVED", "OPEN")))
        assert await _outcome(session_factory, org, "ZD-1244") is True


class TestGithubCodingFlow:
    def _merged(self, number=2, sha="c4295bd74fb0f4fda03689c3df3f2803b658fd85"):
        """DERIVED from the vendor's closed example: merged fields set."""
        payload = _fixture("github_pull_request_closed.json")
        payload["number"] = number
        payload["pull_request"].update(merged=True, merged_at=payload["pull_request"]["closed_at"],
                                       merge_commit_sha=sha)
        return payload

    async def test_merged_then_reverted_inside_the_window_is_a_failure(self, session_factory):
        org = await _org(session_factory)
        cid = await _connector(session_factory, org, "github", GH_SECRET, config={"window_days": 14})
        await _occasion(session_factory, org, "2")
        await _post(session_factory, cid, *_gh(self._merged(), "pull_request", "d-1"))
        assert await _outcome(session_factory, org, "2") is None
        push = _fixture("github_push_master.json")
        push["commits"][0]["message"] = (
            'Revert "x"\n\nThis reverts commit c4295bd74fb0f4fda03689c3df3f2803b658fd85.\n')
        await _post(session_factory, cid, *_gh(push, "push", "d-2"))
        assert await _outcome(session_factory, org, "2") is False

    async def test_merged_and_never_reverted_succeeds_after_the_window(self, session_factory):
        org = await _org(session_factory)
        cid = await _connector(session_factory, org, "github", GH_SECRET, config={"window_days": 14})
        await _occasion(session_factory, org, "2")
        await _post(session_factory, cid, *_gh(self._merged(), "pull_request", "d-1"))
        await service.finalize_matured(session_factory, now=datetime.now(timezone.utc) + timedelta(days=15))
        assert await _outcome(session_factory, org, "2") is True

    async def test_closed_without_merging_is_a_failure(self, session_factory):
        org = await _org(session_factory)
        cid = await _connector(session_factory, org, "github", GH_SECRET)
        await _occasion(session_factory, org, "2")
        await _post(session_factory, cid, *_gh(_fixture("github_pull_request_closed.json"), "pull_request", "d-1"))
        assert await _outcome(session_factory, org, "2") is False

    async def test_a_delivery_without_a_delivery_id_is_refused(self, session_factory):
        org = await _org(session_factory)
        cid = await _connector(session_factory, org, "github", GH_SECRET)
        headers, body = _gh(self._merged(), "pull_request", "")
        del headers["X-GitHub-Delivery"]
        assert (await _post(session_factory, cid, headers, body)).status == 400

    async def test_another_repos_pr_with_the_same_number_is_ignored(self, session_factory):
        org = await _org(session_factory)
        cid = await _connector(session_factory, org, "github", GH_SECRET,
                               config={"repository": "acme/widgets", "window_days": 0})
        await _occasion(session_factory, org, "2")
        await _post(session_factory, cid, *_gh(self._merged(), "pull_request", "d-1"))  # Codertocat/Hello-World
        assert await _outcome(session_factory, org, "2") is None


# --- Dry run ------------------------------------------------------------------------


class TestDryRun:
    async def test_a_new_connector_is_dry_run_and_records_nothing(self, session_factory):
        org = await _org(session_factory)
        cid = await _connector(session_factory, org, config={"window_days": 0}, live=False)
        await _occasion(session_factory, org, "1244")
        result = await _post(session_factory, cid, *_zd(_zd_status("SOLVED", "OPEN")))
        assert result.body["status"] == "dry-run"
        assert result.body["signals"][0]["action"] == "success 1244"
        assert "resolved" not in result.body["signals"][0]
        assert await _outcome(session_factory, org, "1244") is None
        assert await _count(session_factory, PendingOutcome) == 0
        async with session_scope(session_factory) as session:
            row = (await session.execute(select(ConnectorDelivery))).scalar_one()
        assert row.dry_run and row.outcome.startswith("dry-run: success 1244")

    async def test_going_live_processes_a_delivery_first_seen_in_dry_run(self, session_factory):
        org = await _org(session_factory)
        cid = await _connector(session_factory, org, config={"window_days": 0}, live=False)
        await _occasion(session_factory, org, "1244")
        headers, body = _zd(_zd_status("SOLVED", "OPEN"))
        await _post(session_factory, cid, headers, body)
        async with session_scope(session_factory) as session:
            await service.set_live(session, cid, True)
        again = await _post(session_factory, cid, headers, body)
        assert again.body["status"] == "ok"
        assert await _outcome(session_factory, org, "1244") is True

    async def test_a_dry_run_sweep_does_not_record_what_a_live_one_would(self, session_factory):
        org = await _org(session_factory)
        cid = await _connector(session_factory, org, config={"window_days": 7})
        await _occasion(session_factory, org, "1244")
        await _post(session_factory, cid, *_zd(_zd_status("SOLVED", "OPEN")))
        async with session_scope(session_factory) as session:
            await service.set_live(session, cid, False)  # back to dry-run
        await service.finalize_matured(session_factory, now=datetime.now(timezone.utc) + timedelta(days=9))
        assert await _outcome(session_factory, org, "1244") is None


# --- Tenancy and audit ----------------------------------------------------------------


class TestTenancy:
    async def test_the_same_occasion_id_in_two_orgs_never_crosses(self, session_factory):
        a, b = await _org(session_factory, "A"), await _org(session_factory, "B")
        b_conn = await _connector(session_factory, b, secret="b-secret", config={"window_days": 0})
        await _occasion(session_factory, a, "1244")
        await _occasion(session_factory, b, "1244")
        await _post(session_factory, b_conn, *_zd(_zd_status("SOLVED", "OPEN"), secret="b-secret"))
        assert await _outcome(session_factory, b, "1244") is True
        assert await _outcome(session_factory, a, "1244") is None

    async def test_every_recorded_outcome_is_audited_to_the_connector_not_a_person(self, session_factory):
        org = await _org(session_factory)
        cid = await _connector(session_factory, org, config={"window_days": 0})
        await _occasion(session_factory, org, "1244")
        await _post(session_factory, cid, *_zd(_zd_status("SOLVED", "OPEN")))
        async with session_scope(session_factory) as session:
            entries = (await session.execute(
                select(AuditLogEntry).where(AuditLogEntry.action == "connector.outcome"))).scalars().all()
        assert len(entries) == 1 and entries[0].actor == f"connector:zendesk:{cid[:8]}"
        assert entries[0].org_id == org


# --- Management --------------------------------------------------------------------


class TestManagement:
    async def test_the_secret_is_stored_sealed_never_in_the_clear(self, session_factory):
        org = await _org(session_factory)
        cid = await _connector(session_factory, org)
        async with session_scope(session_factory) as session:
            stored = (await session.get(Connector, cid)).secret
            dump = json.dumps([str(e.summary) for e in (await session.execute(select(AuditLogEntry))).scalars()])
        assert ZD_SECRET not in stored and stored.startswith("ctenc:v1:")
        assert ZD_SECRET not in dump

    async def test_a_connector_cannot_be_created_without_encryption_configured(self, session_factory):
        org = await _org(session_factory)
        async with session_scope(session_factory) as session:
            with pytest.raises(service.ConnectorError, match="HUB_ENCRYPTION_KEY"):
                await service.create_connector(
                    session, org, "zendesk", ZD_SECRET, cipher=EnvelopeCipher())

    @pytest.mark.parametrize("provider, secret, options, message", [
        ("salesforce", "s", {}, "unknown provider"),
        ("zendesk", "  ", {}, "signing secret is required"),
        ("zendesk", "s", {"nope": 1}, "unknown config option"),
        ("github", "s", {"repository": "x"}, "owner/name"),
    ])
    async def test_bad_creation_is_refused(self, session_factory, provider, secret, options, message):
        org = await _org(session_factory)
        async with session_scope(session_factory) as session:
            with pytest.raises(service.ConnectorError, match=message):
                await service.create_connector(session, org, provider, secret, cipher=CIPHER, config=options)

    async def test_new_connectors_start_in_dry_run(self, session_factory):
        org = await _org(session_factory)
        cid = await _connector(session_factory, org, live=False)
        async with session_scope(session_factory) as session:
            assert (await session.get(Connector, cid)).dry_run is True

    async def test_the_manage_commands_never_print_the_secret(self, session_factory, monkeypatch, capsys):
        from hub import manage

        org = await _org(session_factory)
        monkeypatch.setenv("HUB_CONNECTOR_SECRET", "super-secret-value")
        monkeypatch.setattr(manage, "_config_cipher", lambda: CIPHER)
        assert await manage.connector_add(org, "zendesk", '{"window_days": 7}', session_factory=session_factory)
        out = capsys.readouterr()
        assert "super-secret-value" not in out.out + out.err
        assert "DRY-RUN" in out.out and "/connectors/" in out.out
        assert await manage.connector_list(org, session_factory=session_factory)
        listing = capsys.readouterr().out
        assert "dry-run" in listing and "super-secret-value" not in listing
        cid = listing.split()[0]
        assert await manage.connector_live(cid, session_factory=session_factory)
        assert "LIVE" in capsys.readouterr().out
        assert not await manage.connector_live("garbage", session_factory=session_factory)
        monkeypatch.delenv("HUB_CONNECTOR_SECRET")
        assert not await manage.connector_add(org, "zendesk", session_factory=session_factory)
        assert "never taken from the command line" in capsys.readouterr().err

    async def test_the_route_is_off_by_default(self):
        assert HubConfig(database_url="x").connectors_enabled is False


# --- The HTTP route -----------------------------------------------------------------


class TestRoute:
    def _client(self, session_factory, config):
        app = Starlette()
        add_connector_routes(app, session_factory, config=config)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def test_a_signed_delivery_over_http_records_the_outcome(self, session_factory, config, monkeypatch):
        monkeypatch.setattr(type(config), "cipher", lambda self: CIPHER)
        org = await _org(session_factory)
        cid = await _connector(session_factory, org, config={"window_days": 0})
        await _occasion(session_factory, org, "1244")
        headers, body = _zd(_zd_status("SOLVED", "OPEN"))
        async with self._client(session_factory, config) as client:
            response = await client.post(f"/connectors/{cid}/events", content=body, headers=headers)
            bad = await client.post(f"/connectors/{cid}/events", content=body,
                                    headers={**headers, "X-Zendesk-Webhook-Signature": "AAAA"})
            only_post = await client.get(f"/connectors/{cid}/events")
        assert response.status_code == 200 and response.json()["status"] == "ok"
        assert bad.status_code == 401 and only_post.status_code == 405
        assert await _outcome(session_factory, org, "1244") is True

    async def test_hostile_connector_ids_never_500(self, session_factory, config, monkeypatch):
        monkeypatch.setattr(type(config), "cipher", lambda self: CIPHER)
        async with self._client(session_factory, config) as client:
            for hostile in ["%27%20OR%201%3D1", "..%2F..%2Fetc", "A" * 1500, "%00", "-1"]:
                response = await client.post(f"/connectors/{hostile}/events", content=b"{}")
                # 401 for anything that reaches the handler; 404 where the path
                # itself does not route (an encoded slash). Never a 5xx.
                assert response.status_code in (401, 404), hostile

    async def test_the_route_is_rate_limited_before_any_work(self, session_factory):
        config = HubConfig(database_url="x", auth_attempts_per_minute=2, auth_attempts_burst=2)
        async with self._client(session_factory, config) as client:
            codes = [(await client.post("/connectors/00000000-0000-0000-0000-000000000000/events",
                                        content=b"{}")).status_code for _ in range(6)]
        assert 429 in codes and codes[0] == 401


async def test_deleting_an_org_removes_its_connectors_ledger_and_pending(session_factory):
    org = await _org(session_factory)
    cid = await _connector(session_factory, org, config={"window_days": 7})
    await _post(session_factory, cid, *_zd(_zd_status("SOLVED", "OPEN")))
    async with session_scope(session_factory) as session:
        await session.execute(update(Organization).where(Organization.id == org).values(name="x"))
        await session.delete(await session.get(Organization, org))
    for model in (Connector, ConnectorDelivery, PendingOutcome):
        assert await _count(session_factory, model) == 0, model




class TestHardening:
    async def test_an_authentic_payload_of_the_wrong_shape_is_acknowledged_not_a_500(self, session_factory):
        org = await _org(session_factory)
        cid = await _connector(session_factory, org)
        payload = _zd_status("SOLVED", "OPEN")
        payload["detail"] = "not an object"
        result = await _post(session_factory, cid, *_zd(payload))
        assert result.status == 200 and result.body["status"] == "unusable"
        assert await _count(session_factory, PendingOutcome) == 0

    async def test_one_delivery_cannot_fan_out_into_unbounded_work(self, session_factory):
        org = await _org(session_factory)
        cid = await _connector(session_factory, org, "github", GH_SECRET, config={"window_days": 14})
        push = _fixture("github_push_master.json")
        push["commits"] = [
            {"message": f"This reverts commit {i:040x}."} for i in range(service.MAX_SIGNALS + 150)
        ]
        result = await _post(session_factory, cid, *_gh(push, "push", "d-big"))
        assert result.status == 200 and len(result.body["signals"]) == service.MAX_SIGNALS

    async def test_the_replay_ledger_is_trimmed_past_its_retention(self, session_factory):
        org = await _org(session_factory)
        cid = await _connector(session_factory, org)
        await _post(session_factory, cid, *_zd(_zd_status("SOLVED", "OPEN")))
        assert await _count(session_factory, ConnectorDelivery) == 1
        soon = datetime.now(timezone.utc) + timedelta(days=service.LEDGER_RETENTION_DAYS - 1)
        await service.finalize_matured(session_factory, now=soon)
        assert await _count(session_factory, ConnectorDelivery) == 1
        late = datetime.now(timezone.utc) + timedelta(days=service.LEDGER_RETENTION_DAYS + 1)
        await service.finalize_matured(session_factory, now=late)
        assert await _count(session_factory, ConnectorDelivery) == 0


class TestGreenhouseRecruitingFlow:
    SECRET = "greenhouse-secret-key"

    def _signed(self, payload, event_id="evt-1"):
        body = json.dumps(payload).encode()
        headers = {"Signature": "sha256 " + hmac.new(self.SECRET.encode(), body, hashlib.sha256).hexdigest()}
        if event_id:
            headers["Greenhouse-Event-ID"] = event_id
        return headers, body

    async def _setup(self, session_factory, application, **config):
        org = await _org(session_factory)
        cid = await _connector(session_factory, org, "greenhouse", self.SECRET, config=config)
        await _occasion(session_factory, org, str(application))
        return org, cid

    async def test_hire_matures_to_success_unless_unhired_inside_the_window(self, session_factory):
        org, cid = await self._setup(session_factory, 46194062, window_days=30)
        hire = _fixture("greenhouse_hire_candidate.json")
        await _post(session_factory, cid, *self._signed(hire, "e1"))
        assert await _outcome(session_factory, org, "46194062") is None
        unhire = copy_with(hire, action="unhire_candidate")
        await _post(session_factory, cid, *self._signed(unhire, "e2"))
        assert await _outcome(session_factory, org, "46194062") is False

    async def test_a_hire_that_holds_succeeds_after_the_window(self, session_factory):
        org, cid = await self._setup(session_factory, 46194062, window_days=30)
        await _post(session_factory, cid, *self._signed(_fixture("greenhouse_hire_candidate.json")))
        await service.finalize_matured(session_factory, now=datetime.now(timezone.utc) + timedelta(days=31))
        assert await _outcome(session_factory, org, "46194062") is True

    async def test_a_rejection_fails_and_a_named_stage_succeeds(self, session_factory):
        org, cid = await self._setup(session_factory, 265293)
        await _post(session_factory, cid, *self._signed(_fixture("greenhouse_reject_candidate.json")))
        assert await _outcome(session_factory, org, "265293") is False

        org2, cid2 = await self._setup(session_factory, 265277, success_stages=["Assessment"])
        await _post(session_factory, cid2, *self._signed(_fixture("greenhouse_candidate_stage_change.json")))
        assert await _outcome(session_factory, org2, "265277") is True

    async def test_the_ping_is_acknowledged_so_greenhouse_does_not_disable_the_webhook(self, session_factory):
        org = await _org(session_factory)
        cid = await _connector(session_factory, org, "greenhouse", self.SECRET)
        result = await _post(session_factory, cid, *self._signed({"configuration": {"url": "x"}}, event_id=""))
        assert result.status == 200 and result.body["signals"] == []

    async def test_a_replayed_delivery_without_an_event_id_is_still_recognised(self, session_factory):
        org, cid = await self._setup(session_factory, 265293)
        headers, body = self._signed(_fixture("greenhouse_reject_candidate.json"), event_id="")
        assert (await _post(session_factory, cid, headers, body)).body["status"] == "ok"
        assert (await _post(session_factory, cid, headers, body)).body["status"] == "duplicate"


def copy_with(payload: dict, **changes) -> dict:
    out = json.loads(json.dumps(payload))
    out.update(changes)
    return out


class TestIntercomSupportFlow:
    SECRET = "intercom-client-secret"

    def _signed(self, topic, conversation="1295", notification="notif-1", age_seconds=30):
        """DERIVED from the vendor's notification envelope example."""
        payload = _fixture("intercom_notification_company_created.json")
        payload.update(topic=topic, id=notification,
                       created_at=int(datetime.now(timezone.utc).timestamp()) - age_seconds)
        payload["data"]["item"] = {"type": "conversation", "id": conversation, "state": "closed"}
        body = json.dumps(payload).encode()
        return {"X-Hub-Signature": "sha1=" + hmac.new(self.SECRET.encode(), body, hashlib.sha1).hexdigest()}, body

    async def _setup(self, session_factory, conversation="1295", **config):
        org = await _org(session_factory)
        cid = await _connector(session_factory, org, "intercom", self.SECRET, config=config)
        await _occasion(session_factory, org, conversation)
        return org, cid

    async def test_closed_matures_to_success(self, session_factory):
        org, cid = await self._setup(session_factory, window_days=7)
        await _post(session_factory, cid, *self._signed("conversation.admin.closed"))
        assert await _outcome(session_factory, org, "1295") is None
        await service.finalize_matured(session_factory, now=datetime.now(timezone.utc) + timedelta(days=8))
        assert await _outcome(session_factory, org, "1295") is True

    async def test_a_customer_reply_after_the_close_is_a_failure(self, session_factory):
        org, cid = await self._setup(session_factory, window_days=7)
        await _post(session_factory, cid, *self._signed("conversation.admin.closed", notification="n1"))
        await _post(session_factory, cid, *self._signed("conversation.user.replied", notification="n2"))
        assert await _outcome(session_factory, org, "1295") is False

    async def test_a_customer_reply_before_any_close_changes_nothing(self, session_factory):
        org, cid = await self._setup(session_factory, window_days=7)
        result = await _post(session_factory, cid, *self._signed("conversation.user.replied"))
        assert "nothing pending" in result.body["signals"][0]["action"]
        assert await _outcome(session_factory, org, "1295") is None

    async def test_a_validly_signed_but_ancient_notification_is_refused(self, session_factory):
        org, cid = await self._setup(session_factory, window_days=0)
        result = await _post(session_factory, cid, *self._signed("conversation.admin.closed", age_seconds=5 * 3600))
        assert result.status == 401
        assert await _outcome(session_factory, org, "1295") is None
