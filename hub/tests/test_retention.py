"""Age-based deletion, and the two things that outrank it.

What these tests defend, in order of how badly getting it wrong would hurt:

1. **A plan deletes nothing.** Everything about this module is built so an
   operator reads the consequences before they happen, and the one bug that
   would make the whole design worthless is a plan that acts.
2. **A stale approval is refused.** The digest names one specific set of
   rows. If the store moved, applying it would delete a set nobody read --
   and a purge is the one operation whose mistakes are unrecoverable and
   invisible, because the evidence is what it deleted.
3. **A legal hold beats a policy, visibly.** Held rows are counted and
   named. A hold that merely skipped them would leave the operator reading
   "purge complete" and believing data was gone that is not.
4. **A running experiment blocks deletion of its arms.** Deleting some
   observations mid-run is differential attrition -- it biases the causal
   estimate invisibly, because the analysis just sees a smaller, apparently
   clean dataset.
5. **Floors are refused, not clamped.** An operator who asked for 7 days and
   silently got 365 believes the store honours a number it does not, and
   finds out from an auditor.
6. **Tenancy.** These are two new org-scoped tables, and a new table that
   skipped row-level security is exactly the hole d5c8b3a91e77 was written
   to close.
"""
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
    """One trace per age in days. Returns their ids, oldest first."""
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
        # The number asked for AND the floor, so the operator can tell which
        # one they got rather than discovering it later.
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
        # Two rows disagreeing about the same objects would make the
        # retention period depend on iteration order.
        assert len(policies) == 1
        assert policies[0].max_age_days == 120
        assert policies[0].note == "legal said so"

    async def test_statuses_are_separate_policies(self, session_factory, org):
        """"Keep quarantined traces two years and ordinary ones ninety days"
        is the shape a real policy takes."""
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
        """The single most important property in this module."""
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
        """Two sets of rows can have the same count; a digest over counts
        would let an apply delete a set the operator never read."""
        await _add_traces(session_factory, org, [400])
        async with session_scope(session_factory) as session:
            await retention.set_policy(session, org, "trace", 90)
        async with session_scope(session_factory) as session:
            first = (await retention.plan(session, org, now=NOW)).digest
        # Same count, different row.
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
        # The store moves: another old trace arrives after the plan was read.
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
        """"The purge ran and deleted nothing" and "the purge never ran" are
        different facts, and only one of them means the schedule is broken."""
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
        # Counted and named, not skipped: an operator reading "purge
        # complete" must not believe data is gone that is not.
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
        # "Frozen from March to July, by whom and why" is the question a
        # hold is ultimately asked; deleting the row loses it.
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
        """Deleting some observations mid-run is differential attrition: it
        biases the estimate invisibly, because the analysis just sees a
        smaller, apparently clean dataset."""
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
        # The export's digest is what the value ledger's signature commits
        # to, and it cannot be recomputed from deleted rows.
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
        """A new tenant-scoped table that skipped RLS is exactly the hole
        d5c8b3a91e77 was written to close. This fails on the NEXT one too,
        which is the point of asserting it against the models rather than
        against a list."""
        from hub.alembic.versions.d5c8b3a91e77_row_level_security import _SCOPED_TABLES
        from hub.alembic.versions.d7f2a63b9c41_retention_policies_and_legal_holds import (
            _NEW_TABLES,
        )
        from hub.models import Base

        protected = {*_SCOPED_TABLES, *_NEW_TABLES, "traces"}
        # Documented exemptions, with the reason each one cannot be scoped.
        # See d5c8b3a91e77's docstring.
        exempt = {
            "api_keys",        # read to DISCOVER the caller's org, pre-auth
            "organizations",   # same pre-auth path
            "audit_log",       # nullable org_id: system events have none
            "trace_relations",  # no org_id; reachable only through traces
        }
        org_scoped = {
            table.name for table in Base.metadata.sorted_tables
            if "org_id" in table.c
        }
        assert org_scoped - exempt - protected == set()

    def test_every_purgeable_kind_names_a_real_model_column(self):
        """A KIND whose timestamp or status column does not exist would
        produce a policy that matches nothing, reported as working."""
        for name, kind in retention.KINDS.items():
            assert hasattr(kind.model, kind.timestamp), name
            assert hasattr(kind.model, "id"), name
            for status, predicate in kind.statuses.items():
                assert predicate() is not None, f"{name}/{status}"

    @pytest.mark.asyncio
    async def test_a_policy_for_a_vanished_type_is_reported_not_ignored(
        self, session_factory, org
    ):
        """A policy the operator believes is running, that silently matches
        nothing, is the worst of both."""
        async with session_scope(session_factory) as session:
            session.add(RetentionPolicy(
                org_id=org, object_type="sometable", status="any", max_age_days=90,
            ))
        async with session_scope(session_factory) as session:
            plan = await retention.plan(session, org, now=NOW)
        assert plan.buckets[0].blocked
        assert "sometable" in plan.render()
