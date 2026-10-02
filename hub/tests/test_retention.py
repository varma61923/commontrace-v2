from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from hub import retention
from hub.db import session_scope
from hub.models import (
    AuditLogEntry,
    HoldoutObservation,
    KnowledgeBaseSubmission,
    LegalHold,
    Organization,
    RetentionPolicy,
    Trace,
)

NOW = datetime(2026, 9, 12, tzinfo=timezone.utc)


def _aged(days: int) -> datetime:
    return NOW - timedelta(days=days)


@pytest_asyncio.fixture
async def org(session_factory):
    async with session_scope(session_factory) as session:
        o = Organization(name="fleet")
        session.add(o)
        await session.flush()
        return o.id


async def _add_traces(session_factory, org_id, ages, **kwargs):
    ids = []
    async with session_scope(session_factory) as session:
        for i, age in enumerate(ages):
            t = Trace(
                org_id=org_id, title=f"t{i}", context_text="c",
                solution_text="s", agent_type="support",
                created_at=_aged(age), **kwargs,
            )
            session.add(t)
            await session.flush()
            ids.append(t.id)
    return ids


@pytest.mark.asyncio
class TestPolicies:
    async def test_a_floor_is_refused_rather_than_clamped(self, session_factory, org):
        async with session_scope(session_factory) as session:
            with pytest.raises(retention.RetentionError) as exc:
                await retention.set_policy(session, org, "audit_log", 7)
        assert "365" in str(exc.value) and "7" in str(exc.value)
        assert "proves a purge happened" in str(exc.value)

    async def test_the_floor_is_a_minimum_not_a_fixed_value(self, session_factory, org):
        async with session_scope(session_factory) as session:
            policy = await retention.set_policy(session, org, "audit_log", 3650)
        assert policy.max_age_days == 3650

    async def test_an_unknown_object_type_lists_the_known_ones(self, session_factory, org):
        async with session_scope(session_factory) as session:
            with pytest.raises(retention.RetentionError) as exc:
                await retention.set_policy(session, org, "lessons", 90)
        assert "trace" in str(exc.value) and "holdout_observation" in str(exc.value)

    async def test_an_unknown_status_lists_the_known_ones(self, session_factory, org):
        async with session_scope(session_factory) as session:
            with pytest.raises(retention.RetentionError) as exc:
                await retention.set_policy(session, org, "trace", 90, status="archived")
        assert "quarantined" in str(exc.value)

    async def test_zero_days_is_refused(self, session_factory, org):
        async with session_scope(session_factory) as session:
            with pytest.raises(retention.RetentionError, match="positive"):
                await retention.set_policy(session, org, "trace", 0)

    async def test_setting_the_same_policy_twice_updates_rather_than_duplicates(
        self, session_factory, org
    ):
        async with session_scope(session_factory) as session:
            await retention.set_policy(session, org, "trace", 90)
        async with session_scope(session_factory) as session:
            await retention.set_policy(session, org, "trace", 120, note="legal said so")
        async with session_scope(session_factory) as session:
            policies = await retention.policies_for(session, org)
        assert len(policies) == 1
        assert policies[0].max_age_days == 120
        assert policies[0].note == "legal said so"

    async def test_statuses_are_separate_policies(self, session_factory, org):
        async with session_scope(session_factory) as session:
            await retention.set_policy(session, org, "trace", 90, status="active")
            await retention.set_policy(session, org, "trace", 730, status="quarantined")
        async with session_scope(session_factory) as session:
            policies = await retention.policies_for(session, org)
        assert {(p.status, p.max_age_days) for p in policies} == {
            ("active", 90), ("quarantined", 730),
        }


@pytest.mark.asyncio
class TestPlanning:
    async def test_no_policy_means_nothing_expires_and_says_so(self, session_factory, org):
        await _add_traces(session_factory, org, [900])
        async with session_scope(session_factory) as session:
            plan = await retention.plan(session, org, now=NOW)
        assert plan.is_empty
        assert "indefinitely" in plan.render()

    async def test_a_plan_deletes_nothing(self, session_factory, org):
        await _add_traces(session_factory, org, [400, 400, 10])
        async with session_scope(session_factory) as session:
            await retention.set_policy(session, org, "trace", 90)
        async with session_scope(session_factory) as session:
            plan = await retention.plan(session, org, now=NOW)
            assert plan.n_doomed == 2
        async with session_scope(session_factory) as session:
            still_there = await session.scalar(
                select(func.count()).select_from(Trace).where(Trace.org_id == org)
            )
        assert still_there == 3
        assert "Nothing has been deleted" in plan.render()

    async def test_only_rows_past_the_cutoff_are_doomed(self, session_factory, org):
        ids = await _add_traces(session_factory, org, [400, 91, 89, 1])
        async with session_scope(session_factory) as session:
            await retention.set_policy(session, org, "trace", 90)
        async with session_scope(session_factory) as session:
            plan = await retention.plan(session, org, now=NOW)
        doomed = set(plan.buckets[0].doomed)
        assert doomed == {ids[0], ids[1]}

    async def test_a_status_policy_leaves_other_statuses_alone(self, session_factory, org):
        plain = await _add_traces(session_factory, org, [400])
        held = await _add_traces(session_factory, org, [400], quarantined=True)
        async with session_scope(session_factory) as session:
            await retention.set_policy(session, org, "trace", 90, status="active")
        async with session_scope(session_factory) as session:
            plan = await retention.plan(session, org, now=NOW)
        assert set(plan.buckets[0].doomed) == set(plain)
        assert held[0] not in plan.buckets[0].doomed

    async def test_another_orgs_data_is_never_in_the_plan(self, session_factory, org):
        async with session_scope(session_factory) as session:
            other = Organization(name="someone else")
            session.add(other)
            await session.flush()
            other_id = other.id
        mine = await _add_traces(session_factory, org, [400])
        theirs = await _add_traces(session_factory, other_id, [400])
        async with session_scope(session_factory) as session:
            await retention.set_policy(session, org, "trace", 90)
        async with session_scope(session_factory) as session:
            plan = await retention.plan(session, org, now=NOW)
        assert set(plan.buckets[0].doomed) == set(mine)
        assert theirs[0] not in plan.buckets[0].doomed

    async def test_the_digest_distinguishes_different_sets_of_the_same_size(
        self, session_factory, org
    ):
        await _add_traces(session_factory, org, [400])
        async with session_scope(session_factory) as session:
            await retention.set_policy(session, org, "trace", 90)
        async with session_scope(session_factory) as session:
            first = (await retention.plan(session, org, now=NOW)).digest
        async with session_scope(session_factory) as session:
            await session.execute(Trace.__table__.delete())
        await _add_traces(session_factory, org, [400])
        async with session_scope(session_factory) as session:
            second = (await retention.plan(session, org, now=NOW)).digest
        assert first != second

    async def test_the_digest_is_stable_across_identical_plans(self, session_factory, org):
        await _add_traces(session_factory, org, [400, 300])
        async with session_scope(session_factory) as session:
            await retention.set_policy(session, org, "trace", 90)
        async with session_scope(session_factory) as session:
            a = await retention.plan(session, org, now=NOW)
            b = await retention.plan(session, org, now=NOW)
        assert a.digest == b.digest


@pytest.mark.asyncio
class TestApply:
    async def _one_policy(self, session_factory, org, days=90):
        async with session_scope(session_factory) as session:
            await retention.set_policy(session, org, "trace", days)

    async def test_apply_deletes_exactly_the_planned_rows(self, session_factory, org):
        ids = await _add_traces(session_factory, org, [400, 400, 10])
        await self._one_policy(session_factory, org)
        async with session_scope(session_factory) as session:
            plan = await retention.plan(session, org, now=NOW)
            await retention.apply(session, org, plan.digest, now=NOW)
        async with session_scope(session_factory) as session:
            left = set(
                (await session.execute(
                    select(Trace.id).where(Trace.org_id == org)
                )).scalars()
            )
        assert left == {ids[2]}

    async def test_a_stale_digest_deletes_nothing(self, session_factory, org):
        await _add_traces(session_factory, org, [400])
        await self._one_policy(session_factory, org)
        async with session_scope(session_factory) as session:
            stale = (await retention.plan(session, org, now=NOW)).digest
        await _add_traces(session_factory, org, [500])
        async with session_scope(session_factory) as session:
            with pytest.raises(retention.StalePlanError) as exc:
                await retention.apply(session, org, stale, now=NOW)
        async with session_scope(session_factory) as session:
            survived = await session.scalar(
                select(func.count()).select_from(Trace).where(Trace.org_id == org)
            )
        assert survived == 2
        assert exc.value.planned == stale
        assert exc.value.current != stale

    async def test_a_purge_is_audited_even_when_it_deletes_nothing(
        self, session_factory, org
    ):
        await self._one_policy(session_factory, org)
        async with session_scope(session_factory) as session:
            plan = await retention.plan(session, org, now=NOW)
            await retention.apply(session, org, plan.digest, now=NOW)
        async with session_scope(session_factory) as session:
            entries = list((await session.execute(
                select(AuditLogEntry).where(AuditLogEntry.action == "retention.purge")
            )).scalars())
        assert len(entries) == 1
        assert "nothing" in entries[0].summary

    async def test_the_audit_row_names_what_went(self, session_factory, org):
        await _add_traces(session_factory, org, [400, 400])
        await self._one_policy(session_factory, org)
        async with session_scope(session_factory) as session:
            plan = await retention.plan(session, org, now=NOW)
            await retention.apply(session, org, plan.digest, now=NOW)
        async with session_scope(session_factory) as session:
            entry = (await session.execute(
                select(AuditLogEntry).where(AuditLogEntry.action == "retention.purge")
            )).scalar_one()
        assert "2 trace" in entry.summary
        assert entry.org_id == org


@pytest.mark.asyncio
class TestLegalHold:
    async def test_a_hold_freezes_rows_the_policy_would_delete(self, session_factory, org):
        await _add_traces(session_factory, org, [400, 400])
        async with session_scope(session_factory) as session:
            await retention.set_policy(session, org, "trace", 90)
            await retention.place_hold(
                session, org, reason="Ohio subpoena 2026-44", placed_by="ops",
            )
        async with session_scope(session_factory) as session:
            plan = await retention.plan(session, org, now=NOW)
        assert plan.n_doomed == 0
        assert plan.n_held == 2
        assert "Ohio subpoena 2026-44" in plan.render()

    async def test_applying_under_a_hold_deletes_nothing(self, session_factory, org):
        await _add_traces(session_factory, org, [400, 400])
        async with session_scope(session_factory) as session:
            await retention.set_policy(session, org, "trace", 90)
            await retention.place_hold(
                session, org, reason="investigation", placed_by="ops",
            )
        async with session_scope(session_factory) as session:
            plan = await retention.plan(session, org, now=NOW)
            await retention.apply(session, org, plan.digest, now=NOW)
        async with session_scope(session_factory) as session:
            survived = await session.scalar(
                select(func.count()).select_from(Trace).where(Trace.org_id == org)
            )
        assert survived == 2

    async def test_a_hold_on_one_object_freezes_only_that_one(self, session_factory, org):
        ids = await _add_traces(session_factory, org, [400, 400])
        async with session_scope(session_factory) as session:
            await retention.set_policy(session, org, "trace", 90)
            await retention.place_hold(
                session, org, reason="named in a claim", placed_by="ops",
                object_type="trace", target_id=ids[0],
            )
        async with session_scope(session_factory) as session:
            plan = await retention.plan(session, org, now=NOW)
        assert plan.buckets[0].doomed == (ids[1],)
        assert plan.n_held == 1

    async def test_a_typed_hold_leaves_other_types_alone(self, session_factory, org):
        await _add_traces(session_factory, org, [400])
        async with session_scope(session_factory) as session:
            session.add(KnowledgeBaseSubmission(
                org_id=org, title="s", context_text="c", solution_text="s",
                agent_type="support", created_at=_aged(400),
            ))
            await retention.set_policy(session, org, "trace", 90)
            await retention.set_policy(session, org, "kb_submission", 90)
            await retention.place_hold(
                session, org, reason="trace dispute", placed_by="ops",
                object_type="trace",
            )
        async with session_scope(session_factory) as session:
            plan = await retention.plan(session, org, now=NOW)
        by_type = {b.object_type: b for b in plan.buckets}
        assert by_type["trace"].n_doomed == 0
        assert by_type["kb_submission"].n_doomed == 1

    async def test_a_hold_needs_a_reason(self, session_factory, org):
        async with session_scope(session_factory) as session:
            with pytest.raises(retention.RetentionError, match="reason"):
                await retention.place_hold(
                    session, org, reason="   ", placed_by="ops",
                )

    async def test_a_targeted_hold_needs_a_type(self, session_factory, org):
        async with session_scope(session_factory) as session:
            with pytest.raises(retention.RetentionError, match="type as well as its id"):
                await retention.place_hold(
                    session, org, reason="r", placed_by="ops", target_id="abc",
                )

    async def test_releasing_keeps_the_row_so_the_freeze_stays_auditable(
        self, session_factory, org
    ):
        async with session_scope(session_factory) as session:
            hold = await retention.place_hold(
                session, org, reason="subpoena", placed_by="ops",
            )
            await session.flush()
            hold_id = hold.id
        async with session_scope(session_factory) as session:
            await retention.release_hold(session, hold_id, reason="matter closed")
        async with session_scope(session_factory) as session:
            row = await session.get(LegalHold, hold_id)
            active = await retention.active_holds(session, org)
        assert row is not None
        assert row.released_at is not None
        assert row.reason == "subpoena"
        assert row.release_reason == "matter closed"
        assert active == []

    async def test_a_released_hold_stops_freezing(self, session_factory, org):
        await _add_traces(session_factory, org, [400])
        async with session_scope(session_factory) as session:
            await retention.set_policy(session, org, "trace", 90)
            hold = await retention.place_hold(
                session, org, reason="subpoena", placed_by="ops",
            )
            await session.flush()
            hold_id = hold.id
        async with session_scope(session_factory) as session:
            await retention.release_hold(session, hold_id)
        async with session_scope(session_factory) as session:
            plan = await retention.plan(session, org, now=NOW)
        assert plan.n_doomed == 1

    async def test_releasing_twice_is_refused(self, session_factory, org):
        async with session_scope(session_factory) as session:
            hold = await retention.place_hold(
                session, org, reason="subpoena", placed_by="ops",
            )
            await session.flush()
            hold_id = hold.id
        async with session_scope(session_factory) as session:
            await retention.release_hold(session, hold_id)
        async with session_scope(session_factory) as session:
            with pytest.raises(retention.RetentionError, match="already released"):
                await retention.release_hold(session, hold_id)


@pytest.mark.asyncio
class TestRunningExperiment:
    async def _observations(self, session_factory, org, n, age=400):
        async with session_scope(session_factory) as session:
            for i in range(n):
                session.add(HoldoutObservation(
                    org_id=org, trace_id=str(uuid.uuid4()),
                    occasion_id=f"occ-{i}", injected=bool(i % 2),
                    succeeded=True, created_at=_aged(age),
                ))

    async def test_a_running_experiment_blocks_deletion_of_its_arms(
        self, session_factory, org
    ):
        await self._observations(session_factory, org, 4)
        async with session_scope(session_factory) as session:
            o = await session.get(Organization, org)
            o.holdout_rate = 0.5
            await retention.set_policy(session, org, "holdout_observation", 90)
        async with session_scope(session_factory) as session:
            plan = await retention.plan(session, org, now=NOW)
        assert plan.n_doomed == 0
        assert plan.buckets[0].blocked == retention.BLOCKED_EXPERIMENT_RUNNING
        assert "differential attrition" in plan.render()

    async def test_the_block_lifts_once_the_experiment_stops(self, session_factory, org):
        await self._observations(session_factory, org, 4)
        async with session_scope(session_factory) as session:
            o = await session.get(Organization, org)
            o.holdout_rate = 0.0
            await retention.set_policy(session, org, "holdout_observation", 90)
        async with session_scope(session_factory) as session:
            plan = await retention.plan(session, org, now=NOW)
        assert plan.n_doomed == 4

    async def test_deleting_observations_warns_about_the_signed_ledger(
        self, session_factory, org
    ):
        await self._observations(session_factory, org, 2)
        async with session_scope(session_factory) as session:
            await retention.set_policy(session, org, "holdout_observation", 90)
        async with session_scope(session_factory) as session:
            plan = await retention.plan(session, org, now=NOW)
        assert "export-assignments" in plan.render()

    async def test_a_running_experiment_does_not_block_other_types(
        self, session_factory, org
    ):
        await _add_traces(session_factory, org, [400])
        async with session_scope(session_factory) as session:
            o = await session.get(Organization, org)
            o.holdout_rate = 0.5
            await retention.set_policy(session, org, "trace", 90)
        async with session_scope(session_factory) as session:
            plan = await retention.plan(session, org, now=NOW)
        assert plan.n_doomed == 1


class TestSchemaSafety:
    def test_every_org_scoped_table_has_row_level_security(self):
        from hub.alembic.versions.a7c3e91d4b20_outcome_connectors import (
            _NEW_TABLES as _CONNECTOR_TABLES,
        )
        from hub.alembic.versions.a7c3e91f4b28_collaboration_comments_assignments import (
            _NEW_TABLES as _COLLAB_TABLES,
        )
        from hub.alembic.versions.b4d9e12a6f37_alert_rules import (
            _NEW_TABLES as _ALERT_TABLES,
        )
        from hub.alembic.versions.c2f8a4d16e93_human_users_and_roles import (
            _NEW_TABLES as _USER_TABLES,
        )
        from hub.alembic.versions.c9a1e73d5f02_scim_groups import (
            _NEW_TABLES as _SCIM_GROUP_TABLES,
        )
        from hub.alembic.versions.d5c8b3a91e77_row_level_security import _SCOPED_TABLES
        from hub.alembic.versions.d7f2a63b9c41_retention_policies_and_legal_holds import (
            _NEW_TABLES as _RETENTION_TABLES,
        )
        from hub.alembic.versions.e9b4c07d15a8_webhook_event_export import (
            _NEW_TABLES as _WEBHOOK_TABLES,
        )
        from hub.models import Base

        protected = {
            *_SCOPED_TABLES, *_RETENTION_TABLES, *_WEBHOOK_TABLES,
            *_USER_TABLES, *_COLLAB_TABLES, *_ALERT_TABLES, *_SCIM_GROUP_TABLES, *_CONNECTOR_TABLES,
            "traces",
        }
        exempt = {
            "api_keys",
            "organizations",
            "audit_log",
            "trace_relations",
        }
        org_scoped = {
            table.name for table in Base.metadata.sorted_tables
            if "org_id" in table.c
        }
        assert org_scoped - exempt - protected == set()

    def test_every_purgeable_kind_names_a_real_model_column(self):
        for name, kind in retention.KINDS.items():
            assert hasattr(kind.model, kind.timestamp), name
            assert hasattr(kind.model, "id"), name
            for status, predicate in kind.statuses.items():
                assert predicate() is not None, f"{name}/{status}"

    @pytest.mark.asyncio
    async def test_a_policy_for_a_vanished_type_is_reported_not_ignored(
        self, session_factory, org
    ):
        async with session_scope(session_factory) as session:
            session.add(RetentionPolicy(
                org_id=org, object_type="sometable", status="any", max_age_days=90,
            ))
        async with session_scope(session_factory) as session:
            plan = await retention.plan(session, org, now=NOW)
        assert plan.buckets[0].blocked
        assert "sometable" in plan.render()
