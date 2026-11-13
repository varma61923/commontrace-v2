from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select, update
from starlette.applications import Starlette

from hub import auth, crud, learning, rest
from hub.abuse import make_rate_limiter
from hub.db import session_scope
from hub.models import (
    AuditLogEntry,
    HoldoutObservation,
    LearningDecision,
    LearningOccasion,
    LearningWork,
    Organization,
    Trace,
)
from unimem import learning_contract as contract

pytestmark = pytest.mark.asyncio


async def setup(session_factory, mode="observe"):
    async with session_scope(session_factory) as session:
        org = Organization(name="learning")
        session.add(org)
        await session.flush()
        source = Trace(
            org_id=org.id,
            title="parser",
            context_text="bad input",
            solution_text="validate first",
            tags=[],
            agent_type="code",
        )
        session.add(source)
        await session.flush()
        org_id, source_id = org.id, source.id
    configured = await learning.operate(session_factory, org_id, "agent:1", "configure", {"mode": mode}, admin=True)
    return org_id, source_id, configured["experiment_id"]


async def assign(session_factory, org_id, source_id, occasion="o", principal="agent:1", **extra):
    return await learning.operate(
        session_factory, org_id, principal, "assign", {"occasion_id": occasion, "memory_ids": [source_id], **extra}
    )


async def test_worker_revalidates_large_source_sets_without_exceeding_driver_bind_limits(session_factory):
    org, source, _ = await setup(session_factory)
    # Evaluation groups sources from many valid <=100-source assignments.
    # Historical IDs remain in evidence after source erasure.
    identities = [str(uuid.UUID(int=index + 1)) for index in range(33000)]
    identities.insert(17000, source)
    async with session_scope(session_factory, org_id=org) as session:
        resolved = await learning._sources(session, session_factory, org, identities, {}, allow_missing=True)
        assert [row["id"] for row in resolved] == [source]


async def test_retry_principal_scope_and_frozen_sources(session_factory):
    org, source, experiment = await setup(session_factory)
    first = await assign(session_factory, org, source)
    assert first == await assign(session_factory, org, source)
    assert "sources" not in first
    assert [memory["id"] for memory in first["memories"]] == first["inject"]
    async with session_scope(session_factory, org_id=org) as session:
        row = (await session.execute(select(LearningOccasion))).scalar_one()
        assert "text" not in row.evidence["assignment"]["sources"][0]
    await assign(session_factory, org, source, principal="agent:2")
    with pytest.raises(contract.LearningError):
        await learning.operate(session_factory, org, "agent:3", "outcome", {"occasion_id": "o", "succeeded": True})
    async with session_scope(session_factory, org_id=org) as session:
        await session.execute(update(Trace).where(Trace.id == source).values(solution_text="changed"))
    with pytest.raises(contract.LearningError, match="conflicts"):
        await assign(session_factory, org, source)
    with pytest.raises(contract.LearningError, match="changed"):
        await learning.operate(
            session_factory, org, "agent:1", "delivery", {"occasion_id": "o", "memory_ids": first["inject"]}
        )
    async with session_scope(session_factory) as session:
        other = Organization(name="other")
        session.add(other)
        await session.flush()
        other_id = other.id
    await learning.operate(session_factory, other_id, "agent:1", "configure", {"mode": "observe"}, admin=True)
    with pytest.raises(contract.LearningUnavailable):
        await assign(session_factory, other_id, source)
    assert experiment == first["experiment_id"]


@pytest.mark.parametrize("wrong_scope", [{"agent": "b"}, {"agent": "a", "labels": ["other"]}, {}])
async def test_delivery_and_outcome_cannot_cross_assignment_scope(session_factory, wrong_scope):
    org, source, experiment = await setup(session_factory)
    original_scope = {"agent": "a", "labels": ["first", "shared"]}
    assignment = await learning.operate(
        session_factory,
        org,
        "agent:1",
        "assign",
        {"occasion_id": "scoped", "memory_ids": [source]},
        scope=original_scope,
    )
    for operation, body in (
        ("delivery", {"memory_ids": assignment["inject"]}),
        ("outcome", {"succeeded": True}),
    ):
        with pytest.raises(contract.LearningError) as refused:
            await learning.operate(
                session_factory,
                org,
                "agent:1",
                operation,
                {"occasion_id": "scoped", **body},
                scope=wrong_scope,
            )
        assert refused.value.status_code == 404
    async with session_scope(session_factory, org_id=org) as session:
        row = await session.get(LearningOccasion, (org, experiment, "agent:1", "scoped"))
        assert row.evidence["delivery"] is None and row.evidence["outcome"] is None
        assert await session.get(LearningWork, (org, experiment)) is None
    # Equivalent normalized label ordering is the original authority, not a new scope.
    equivalent_scope = {"agent": "a", "labels": ["shared", "first"]}
    for operation, body in (
        ("delivery", {"memory_ids": assignment["inject"]}),
        ("outcome", {"succeeded": True}),
    ):
        result = await learning.operate(
            session_factory,
            org,
            "agent:1",
            operation,
            {"occasion_id": "scoped", **body},
            scope=equivalent_scope,
        )
        assert result[operation] is not None


async def test_admin_learning_actions_are_transactionally_audited_without_payloads(session_factory):
    org, source, experiment = await setup(session_factory)
    await assign(session_factory, org, source)
    await learning.operate(session_factory, org, "owner:reviewer", "run", admin=True)
    await learning.operate(
        session_factory,
        org,
        "owner:reviewer",
        "rollback",
        {"reason": "sensitive operational details must stay in revision evidence"},
        admin=True,
    )
    async with session_scope(session_factory, org_id=org) as session:
        rows = (await session.execute(select(AuditLogEntry).order_by(AuditLogEntry.created_at))).scalars().all()
        assert [row.action for row in rows] == ["learning.configure", "learning.run", "learning.rollback"]
        assert [row.actor for row in rows] == ["agent:1", "owner:reviewer", "owner:reviewer"]
        assert all(row.org_id == org and row.target_id == experiment for row in rows)
        assert all(row.target_type == "learning_experiment" for row in rows)
        summaries = " ".join(row.summary for row in rows)
        assert "applied=False" in summaries and "restored_revision=0" in summaries
        assert "sensitive" not in summaries and source not in summaries and "validate first" not in summaries
    # Failed validation/authorization is not recorded as a successful mutation.
    with pytest.raises(PermissionError):
        await learning.operate(session_factory, org, "reader", "configure", {"mode": "adaptive"})
    with pytest.raises(contract.LearningError):
        await learning.operate(
            session_factory, org, "owner:reviewer", "rollback", {"reason": "x", "decision_revision": 999}, admin=True
        )
    async with session_scope(session_factory, org_id=org) as session:
        assert len((await session.execute(select(AuditLogEntry))).scalars().all()) == 3


@pytest.mark.parametrize("operation", ["configure", "run", "rollback"])
async def test_audit_failure_rolls_back_learning_administration(session_factory, monkeypatch, operation):
    from hub.models import LearningExperiment

    org, source, experiment = await setup(session_factory)
    assignment = await assign(session_factory, org, source)
    await learning.operate(
        session_factory, org, "agent:1", "delivery", {"occasion_id": "o", "memory_ids": assignment["inject"]}
    )
    await learning.operate(session_factory, org, "agent:1", "outcome", {"occasion_id": "o", "succeeded": True})

    async def unavailable(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(learning.audit, "record", unavailable)
    body = (
        {"mode": "adaptive"} if operation == "configure" else {"reason": "restore"} if operation == "rollback" else {}
    )
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await learning.operate(session_factory, org, "owner", operation, body, admin=True)
    async with session_scope(session_factory, org_id=org) as session:
        experiments = (await session.execute(select(LearningExperiment))).scalars().all()
        assert len(experiments) == 1 and experiments[0].active
        assert experiments[0].checkpoint is None and experiments[0].report is None
        assert (await session.get(LearningWork, (org, experiment))).pending
        assert len((await session.execute(select(LearningDecision))).scalars().all()) == 1
        assert len((await session.execute(select(AuditLogEntry))).scalars().all()) == 1


async def test_concurrent_outcome_exactly_once_and_lease_fencing(session_factory):
    org, source, _ = await setup(session_factory)
    assignment = await assign(session_factory, org, source)
    await learning.operate(
        session_factory, org, "agent:1", "delivery", {"occasion_id": "o", "memory_ids": assignment["inject"]}
    )

    async def outcome(value):
        return await learning.operate(session_factory, org, "agent:1", "outcome", {"occasion_id": "o", "reward": value})

    results = await asyncio.gather(outcome(0.75), outcome(0.75))
    assert results[0] == results[1]
    with pytest.raises(contract.LearningError, match="conflicts"):
        await outcome(0.25)
    first = await learning.claim_work(session_factory)
    assert first
    assert await learning.claim_work(session_factory) is None
    async with session_scope(session_factory, org_id=org) as session:
        await session.execute(
            update(LearningWork).values(lease_until=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
    second = await learning.claim_work(session_factory)
    assert second and second[3] != first[3]
    assert await learning.complete_work(session_factory, first) is False
    assert await learning.complete_work(session_factory, second) is True
    status = await learning.operate(session_factory, org, "agent:1", "status")
    assert status["outcomes"] == 1 and not status["pending_work"]


async def test_delivery_incomplete_blocks_evaluation_and_rollback(session_factory):
    org, source, _ = await setup(session_factory, "adaptive")
    # Exact receipt is required even for a randomized control occasion.
    await assign(session_factory, org, source)
    await learning.operate(session_factory, org, "agent:1", "outcome", {"occasion_id": "o", "succeeded": True})
    result = await learning.operate(session_factory, org, "agent:1", "run", admin=True)
    assert "incomplete_delivery" in result["blocked"]
    assert not result["applied"]
    previous = result["decision_revision"]
    rolled = await learning.operate(
        session_factory, org, "agent:1", "rollback", {"reason": "operator reset", "decision_revision": 0}, admin=True
    )
    assert rolled["decision_revision"] > previous
    async with session_scope(session_factory, org_id=org) as session:
        assert len((await session.execute(select(LearningDecision))).scalars().all()) == 2
        assert (await session.execute(select(LearningOccasion))).scalar_one().evidence["outcome"] is not None


async def test_off_does_not_randomize_and_revocation_blocks_delivery(session_factory):
    org, source, _ = await setup(session_factory, "off")
    assignment = await assign(session_factory, org, source)
    assert assignment["inject"] == [source]
    async with session_scope(session_factory, org_id=org) as session:
        await session.execute(update(Trace).where(Trace.id == source).values(quarantined=True))
    with pytest.raises(contract.LearningUnavailable):
        await learning.operate(
            session_factory, org, "agent:1", "delivery", {"occasion_id": "o", "memory_ids": [source]}
        )


async def test_legacy_metadata_is_frozen_and_unknown_not_invented(session_factory):
    org, source, _ = await setup(session_factory)
    async with session_scope(session_factory, org_id=org) as session:
        await session.execute(
            update(Organization).where(Organization.id == org).values(holdout_rate=0.3, holdout_salt="frozen")
        )
        await crud.holdout_assign(session, org, [source], "new")
        session.add(HoldoutObservation(org_id=org, trace_id=source, occasion_id="old", injected=True, salt="frozen"))
    async with session_scope(session_factory, org_id=org) as session:
        await session.execute(update(Organization).where(Organization.id == org).values(holdout_rate=0.7))
        assignments = await crud.holdout_assignments(session, org)
        assert len(assignments) == 1 and assignments[0].rate == 0.3


async def test_http_scopes_strict_json_and_mcp_registration(session_factory, config):
    org, source, _ = await setup(session_factory)
    async with session_scope(session_factory) as session:
        issued = await auth.issue_api_key(session, org, scopes=["read"])
        raw = issued.raw_key
    app = Starlette()
    rest.add_rest_routes(app, session_factory, config=config, rate_limiter=make_rate_limiter(config))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        headers = {"X-API-Key": raw}
        response = await client.get("/v1/learning/capabilities", headers=headers)
        assert response.status_code == 200 and response.json()["schema"] == contract.SCHEMA
        assert (
            await client.post("/v1/learning/configure", headers=headers, json={"mode": "adaptive"})
        ).status_code == 403
        assert (
            await client.post("/v1/learning/outcome", headers=headers, json={"occasion_id": "o", "succeeded": True})
        ).status_code == 403
        assert (
            await client.post("/v1/learning/assign", headers=headers, content=b'{"occasion_id":"o","occasion_id":"x"}')
        ).status_code == 400
        assert (
            await client.post(
                "/v1/learning/assign", headers=headers, json={"occasion_id": "o", "memory_ids": [source], "org_id": org}
            )
        ).status_code == 400
        response = await client.post(
            "/v1/learning/assign", headers=headers, json={"occasion_id": "o", "memory_ids": [source]}
        )
        assert response.status_code == 200
    from hub.server import build_mcp_server

    mcp = build_mcp_server(config, session_factory, make_rate_limiter(config))
    assert {"learning_" + operation for operation in contract.OPERATIONS} <= set(mcp.unimem_tool_scopes)
    assert mcp.unimem_tool_scopes["learning_configure"] == "admin"


async def test_delayed_metrics_freeze_utility_and_worker_generation(session_factory):
    org, source, _ = await setup(session_factory)
    old = await learning.operate(
        session_factory,
        org,
        "agent:1",
        "configure",
        {"mode": "observe", "utility": {"terms": {"speed": {"low": 0, "high": 100}}}},
        admin=True,
    )
    assignment = await assign(session_factory, org, source)
    await learning.operate(
        session_factory,
        org,
        "agent:1",
        "configure",
        {"mode": "observe", "utility": {"terms": {"speed": {"low": 0, "high": 200}}}},
        admin=True,
    )
    result = await learning.operate(
        session_factory,
        org,
        "agent:1",
        "outcome",
        {"occasion_id": "o", "experiment_id": old["experiment_id"], "metrics": {"speed": 50}},
    )
    assert result["outcome"]["reward"] == 0.5
    assert result["outcome"]["utility_digest"] == assignment["utility_digest"]
    claim = await learning.claim_work(session_factory)
    # A new receipt invalidates an acquired lease but never loses the work.
    await learning.operate(
        session_factory,
        org,
        "agent:1",
        "delivery",
        {"occasion_id": "o", "experiment_id": old["experiment_id"], "memory_ids": assignment["inject"]},
    )
    assert not await learning.complete_work(session_factory, claim)
    replacement = await learning.claim_work(session_factory)
    assert replacement[2] > claim[2]
    assert await learning.complete_work(session_factory, replacement)


@pytest.mark.parametrize(
    "mode,harmful,applied,worker",
    [
        ("adaptive", False, True, False),
        ("adaptive", True, True, False),
        ("observe", False, False, False),
        ("adaptive", True, True, True),
    ],
)
async def test_actual_sql_measured_policy_and_context(session_factory, mode, harmful, applied, worker):
    from hub.models import LearningExperiment

    org, source, experiment = await setup(session_factory, mode)
    async with session_scope(session_factory, org_id=org) as session:
        row = await session.get(LearningExperiment, (org, experiment))
        row.config = {**row.config, "salt": "deterministic-hosted-learning-test"}
    for index in range(180):
        occasion = f"task-{index}"
        assignment = await assign(session_factory, org, source, occasion, task_context="code")
        await learning.operate(
            session_factory, org, "agent:1", "delivery", {"occasion_id": occasion, "memory_ids": assignment["inject"]}
        )
        await learning.operate(
            session_factory,
            org,
            "agent:1",
            "outcome",
            {"occasion_id": occasion, "succeeded": bool(assignment["inject"]) != harmful},
        )
    await assign(session_factory, org, source, "pending-tail", task_context="code")
    if worker:
        claim = await learning.claim_work(session_factory)
        assert claim and await learning.complete_work(session_factory, claim)
        worker_status = await learning.operate(session_factory, org, "operator", "status", admin=True)
        assert worker_status["decision_revision"] == 1 and not worker_status["pending_work"]
        report = worker_status["report"]
    else:
        report = await learning.operate(session_factory, org, "agent:1", "run", admin=True)
        assert report["applied"] is applied, report
    assert "pending_outcomes" in report["blocked"]
    assert report["decisions"][0]["n_injected"] + report["decisions"][0]["n_withheld"] == 180
    async with session_scope(session_factory, org_id=org) as session:
        policy_audits = (
            (await session.execute(select(AuditLogEntry).where(AuditLogEntry.action == "learning.policy_update")))
            .scalars()
            .all()
        )
        assert len(policy_audits) == int(applied)
        if applied:
            assert policy_audits[0].actor == ("service:internal" if worker else "agent:1")
            assert policy_audits[0].target_id == experiment
            assert "decision_revision=1" in policy_audits[0].summary and source not in policy_audits[0].summary
    status = await learning.operate(session_factory, org, "agent:1", "status")
    if applied:
        selected = next(iter(status["policy"].values()))
        assert selected["disabled"] is harmful
        assert report["decisions"][0]["task_context"] == "code"
        # A policy for code work cannot silently govern a different context.
        other = await assign(session_factory, org, source, "unseen-context", task_context="robot")
        assert other["arms"][0]["rate"] == 0.5
        repeated = await learning.operate(session_factory, org, "agent:1", "run", admin=True)
        assert not repeated["applied"]
    async with session_scope(session_factory, org_id=org) as session:
        await session.execute(update(Trace).where(Trace.id == source).values(quarantined=True))
    stale = await learning.operate(session_factory, org, "agent:1", "run", admin=True)
    assert source in stale["stale_sources"]
    assert not stale["applied"]


async def test_record_grounding_scopes_and_large_unrelated_namespace(session_factory):
    from sqlalchemy.dialects.postgresql import insert

    from hub.memory_records import PostgresJournal
    from hub.models import MemoryRecordProjection
    from unimem.memory_record import MemoryRecord

    org, _, _ = await setup(session_factory)
    records = [
        MemoryRecord(f"unrelated-{i}", "fact", 1, {"text": "unrelated"}, scope={"tenant": org}) for i in range(2100)
    ]
    records += [MemoryRecord("private", "fact", 1, {"temperature": 30}, scope={"tenant": org, "agent": "robot"})]
    async with session_scope(session_factory, org_id=org) as session:
        await session.execute(
            insert(MemoryRecordProjection),
            [
                {"org_id": org, "id": row.id, "version": row.version, "scope": row.scope, "record": row.to_dict()}
                for row in records
            ],
        )
    with pytest.raises(contract.LearningUnavailable):
        await learning.operate(
            session_factory, org, "agent:1", "assign", {"occasion_id": "private", "memory_ids": ["private"]}
        )
    assignment = await learning.operate(
        session_factory,
        org,
        "agent:1",
        "assign",
        {"occasion_id": "private", "memory_ids": ["private"]},
        scope={"agent": "robot"},
    )
    assert assignment["arms"][0]["revision"]
    await PostgresJournal(session_factory, org).forget("private", reason="erasure", scope={"agent": "robot"})
    with pytest.raises(contract.LearningUnavailable):
        await learning.operate(
            session_factory,
            org,
            "agent:1",
            "delivery",
            {"occasion_id": "private", "memory_ids": assignment["inject"]},
            scope={"agent": "robot"},
        )


async def test_status_does_not_expose_other_principal_or_scope(session_factory):
    from hub.models import LearningExperiment

    org, source, experiment = await setup(session_factory)
    await assign(session_factory, org, source, task_context="secret-context")
    async with session_scope(session_factory, org_id=org) as session:
        occasion = (await session.execute(select(LearningOccasion))).scalar_one()
        arm = occasion.evidence["assignment"]["arms"][0]
        key = contract.policy_key(source, arm["revision"], "secret-context")
        row = await session.get(LearningExperiment, (org, experiment))
        row.report = {
            "decisions": [{"key": key, "id": source, "task_context": "secret-context"}],
            "evidence": 1,
            "stale_sources": [],
            "blocked": [],
        }
        session.add(
            LearningDecision(
                org_id=org,
                experiment_id=experiment,
                revision=1,
                decision={"policy": {key: {"rate": 0.05, "disabled": False}}, "report": row.report},
            )
        )
    hidden = await learning.operate(session_factory, org, "agent:2", "status")
    assert hidden["occasions"] == 0 and not hidden["policy"]
    assert not hidden["report"]["decisions"] and "secret-context" not in str(hidden)
    hidden_context = await learning.operate(session_factory, org, "agent:1", "status", scope={"agent": "other"})
    assert hidden_context["occasions"] == 0 and not hidden_context["policy"]
    visible = await learning.operate(session_factory, org, "agent:1", "status")
    assert key in visible["policy"]
    admin = await learning.operate(session_factory, org, "operator", "status", admin=True)
    assert admin["report"]["decisions"][0]["task_context"] == "secret-context"


async def test_shared_policy_keys_do_not_disclose_other_principals_pooled_outcomes(session_factory):
    from sqlalchemy.dialects.postgresql import insert

    from hub.models import LearningExperiment

    org, source, experiment = await setup(session_factory, "adaptive")
    async with session_scope(session_factory, org_id=org) as session:
        configured = await session.get(LearningExperiment, (org, experiment))
        sources = await learning._sources(session, session_factory, org, [source], {"tenant": org})
        saved = []
        for principal in ("agent:1", "agent:2"):
            for i in range(180):
                occasion = str(i)
                unit = contract.digest([principal, occasion])
                assignment = contract.make_assignment(configured.config, unit, sources, task_context="shared")
                assignment["occasion_id"] = occasion
                assignment["sources"] = [{k: v for k, v in s.items() if k != "text"} for s in sources]
                saved.append(
                    {
                        "org_id": org,
                        "experiment_id": experiment,
                        "principal": principal,
                        "occasion_id": occasion,
                        "evidence": {
                            "scope": {"tenant": org},
                            "assignment": assignment,
                            "delivery": assignment["inject"],
                            "outcome": contract.normalize_outcome(
                                configured.config,
                                unit,
                                {"occasion_id": occasion, "succeeded": bool(assignment["inject"])},
                            ),
                        },
                    }
                )
        await session.execute(insert(LearningOccasion), saved)
    evaluated = await learning.operate(session_factory, org, "operator", "run", admin=True)
    assert evaluated["decisions"] and evaluated["evidence"] == 360
    for principal in ("agent:1", "agent:2"):
        status = await learning.operate(session_factory, org, principal, "status")
        assert status["occasions"] == status["outcomes"] == status["report"]["evidence"] == 180
        assert status["policy"]
        assert status["report"]["decisions"] == []
        assert "ci_low" not in str(status) and "n_injected" not in str(status)
    admin = await learning.operate(session_factory, org, "operator", "status", admin=True)
    decision = admin["report"]["decisions"][0]
    expected = evaluated["decisions"][0]
    assert decision["key"] == expected["key"]
    assert decision["effect"] == pytest.approx(expected["effect"])
    assert decision["ci_low"] == pytest.approx(expected["ci_low"])
    assert decision["n_injected"] == expected["n_injected"]
    assert decision["n_withheld"] == expected["n_withheld"]
    assert admin["report"]["evidence"] == 360


async def test_same_occasion_principals_have_independent_randomized_units(session_factory):
    org, source, _ = await setup(session_factory)
    arms = []
    for index in range(20):
        assignment = await assign(session_factory, org, source, principal=f"agent:{index}")
        arms.append(bool(assignment["inject"]))
    assert any(arms) and not all(arms)
    async with session_scope(session_factory, org_id=org) as session:
        rows = (await session.execute(select(LearningOccasion))).scalars().all()
        assert len(rows) == 20


async def test_budget_failure_keeps_durable_work_and_initial_rollback(session_factory):
    org, source, _ = await setup(session_factory)
    initial = await learning.operate(
        session_factory, org, "operator", "rollback", {"reason": "initial baseline"}, admin=True
    )
    assert initial["restored_revision"] == 0
    await assign(session_factory, org, source, "one")
    await assign(session_factory, org, source, "two")
    await learning.operate(session_factory, org, "agent:1", "outcome", {"occasion_id": "one", "succeeded": False})
    result = await learning.operate(session_factory, org, "operator", "run", {"limit": 1}, admin=True)
    assert result["blocked"] == ["evidence_budget_exceeded"] and not result["applied"]
    status = await learning.operate(session_factory, org, "agent:1", "status")
    assert status["pending_work"] and status["failures"] == ["evidence_budget_exceeded"]


async def test_worker_settles_unchanged_checkpoint_and_permanent_budget_refusal(session_factory):
    from sqlalchemy.dialects.postgresql import insert

    org, source, experiment = await setup(session_factory)
    assignment = await assign(session_factory, org, source)
    await learning.operate(
        session_factory, org, "agent:1", "delivery", {"occasion_id": "o", "memory_ids": assignment["inject"]}
    )
    await learning.operate(session_factory, org, "agent:1", "outcome", {"occasion_id": "o", "succeeded": True})
    assert await learning.complete_work(session_factory, await learning.claim_work(session_factory))
    async with session_scope(session_factory, org_id=org) as session:
        await learning._enqueue(session, org, experiment)
    assert await learning.complete_work(session_factory, await learning.claim_work(session_factory))
    assert await learning.claim_work(session_factory) is None
    async with session_scope(session_factory, org_id=org) as session:
        evidence = (await session.execute(select(LearningOccasion))).scalar_one().evidence
        await session.execute(
            insert(LearningOccasion),
            [
                {
                    "org_id": org,
                    "experiment_id": experiment,
                    "principal": "agent:1",
                    "occasion_id": f"budget-{index}",
                    "evidence": evidence,
                }
                for index in range(10000)
            ],
        )
        await learning._enqueue(session, org, experiment)
    assert not await learning.complete_work(session_factory, await learning.claim_work(session_factory))
    assert await learning.claim_work(session_factory) is None
    status = await learning.operate(session_factory, org, "operator", "status", admin=True)
    assert not status["pending_work"] and status["failures"] == ["evidence_budget_exceeded"]


async def test_actor_policy_revocation_cannot_be_bypassed_by_another_principal(session_factory):
    from sqlalchemy.dialects.postgresql import insert

    from hub.models import LearningExperiment, MemoryPolicyState
    from unimem import frontier_protocol as fp

    org, source, experiment = await setup(session_factory, "adaptive")
    async with session_scope(session_factory, org_id=org) as session:
        exp = await session.get(LearningExperiment, (org, experiment))
        exp.config = {**exp.config, "salt": "fixed-actor-revocation-test"}
        sources = await learning._sources(session, session_factory, org, [source], {"tenant": org})
        saved = []
        for principal in ("agent:1", "agent:2"):
            for index in range(180):
                occasion = str(index)
                unit = contract.digest([principal, occasion])
                assignment = contract.make_assignment(exp.config, unit, sources, task_context="code")
                assignment["occasion_id"] = occasion
                assignment["sources"] = [
                    {key: value for key, value in item.items() if key != "text"} for item in sources
                ]
                saved.append(
                    {
                        "org_id": org,
                        "experiment_id": experiment,
                        "principal": principal,
                        "occasion_id": occasion,
                        "evidence": {
                            "scope": {"tenant": org},
                            "assignment": assignment,
                            "delivery": assignment["inject"],
                            "outcome": contract.normalize_outcome(
                                exp.config, unit, {"occasion_id": occasion, "succeeded": bool(assignment["inject"])}
                            ),
                        },
                    }
                )
        await session.execute(insert(LearningOccasion), saved)
        policy = {
            "version": 1,
            "domain": {
                "principals": [
                    {"id": principal, "class": "agent", "attributes": {}}
                    for principal in ("agent:1", "agent:2", "service:internal")
                ],
                "actions": ["read"],
                "memory_classes": ["trace"],
                "label_sets": [[]],
                "purposes": ["default"],
            },
            "rules": [
                {
                    "id": "read",
                    "effect": "allow",
                    "principals": ["agent:2", "service:internal"],
                    "actions": ["read"],
                    "memory_classes": ["trace"],
                }
            ],
        }
        session.add(
            MemoryPolicyState(
                org_id=org,
                generation=1,
                policy=policy,
                activation={"record": {"generation": 1, "policy_hash": fp.digest(policy)}},
            )
        )
    result = await learning.operate(session_factory, org, "service:internal", "run", admin=True)
    assert result["decisions"], result
    assert not result["applied"]
    assert source in result["stale_sources"]
    status = await learning.operate(session_factory, org, "operator", "status", admin=True)
    assert not status["policy"]


async def test_http_to_mcp_retry_same_uuid_principal_and_strict_parameters(session_factory, config):
    from hub.server import build_mcp_server
    from hub.tests.test_api_key_scopes import payload as mcp_payload

    org, source, experiment = await setup(session_factory)
    async with session_scope(session_factory) as session:
        issued = await auth.issue_api_key(session, org, scopes=["read", "write", "admin"])
        principal = await auth.verify_api_key(session, issued.raw_key)
    app = Starlette()
    rest.add_rest_routes(app, session_factory, config=config, rate_limiter=make_rate_limiter(config))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/learning/assign",
            headers={"X-API-Key": issued.raw_key},
            json={"occasion_id": "same", "memory_ids": [source], "context": []},
        )
        assert response.status_code == 200
        first = response.json()
    mcp = build_mcp_server(config, session_factory, make_rate_limiter(config))
    org_token, key_token = auth.current_org_id.set(org), auth.current_key_id.set(principal.key_id)
    scopes_token = auth.current_scopes.set(("read", "write", "admin"))
    try:
        repeated = mcp_payload(
            await mcp.call_tool(
                "learning_assign", {"occasion_id": "same", "memory_ids": [source], "experiment_id": experiment}
            )
        )
        assert repeated == first
        invalid = mcp_payload(await mcp.call_tool("learning_status", {"experiment_id": ""}))
        assert invalid["status"] == 400
        with pytest.raises(Exception, match="valid boolean|validation|Validation"):
            await mcp.call_tool("learning_outcome", {"occasion_id": "same", "succeeded": "yes"})
    finally:
        auth.current_scopes.reset(scopes_token)
        auth.current_key_id.reset(key_token)
        auth.current_org_id.reset(org_token)


@pytest.mark.parametrize(
    "kind,payload",
    [
        ("directive", {"text": "Never leak secrets"}),
        ("fact", {"text": "Explicitly protected", "protected": True}),
        ("profile", {"text": "Critical limit", "critical": True}),
        ("profile", {"text": "Critical legacy limit", "critical": "required"}),
        ("lesson", {"text": "Safe action", "lesson_kind": "directive"}),
    ],
)
async def test_protected_memory_governance_channels_are_never_randomized(session_factory, kind, payload):
    from hub.memory_records import PostgresJournal
    from unimem.memory_record import MemoryRecord

    org, _, _ = await setup(session_factory)
    await PostgresJournal(session_factory, org).put(MemoryRecord("mandatory", kind, 1, payload))
    assignment = await learning.operate(
        session_factory, org, "agent:1", "assign", {"occasion_id": "required", "memory_ids": ["mandatory"]}
    )
    assert assignment["inject"] == ["mandatory"] and assignment["arms"][0]["rate"] == 0


@pytest.mark.parametrize("existing_policy", [False, True])
@pytest.mark.parametrize("phase", ["assign", "run"])
async def test_policy_activation_cannot_interleave_learning_authority(
    session_factory, monkeypatch, existing_policy, phase
):
    from hub import policies
    from hub.models import MemoryPolicyState
    from unimem import frontier_protocol as fp

    org, source, _ = await setup(session_factory)
    if existing_policy:
        async with session_scope(session_factory, org_id=org) as session:
            session.add(MemoryPolicyState(org_id=org, generation=0))
    if phase == "run":
        await assign(session_factory, org, source)
    resolved, release, activation_started, activated = (asyncio.Event() for _ in range(4))
    original = learning._sources
    first = True

    async def blocked_sources(*args, **kwargs):
        nonlocal first
        result = await original(*args, **kwargs)
        if first:
            first = False
            resolved.set()
            await release.wait()
        return result

    monkeypatch.setattr(learning, "_sources", blocked_sources)
    if phase == "assign":
        inflight = asyncio.create_task(assign(session_factory, org, source, "inflight"))
    else:
        inflight = asyncio.create_task(learning.operate(session_factory, org, "operator", "run", admin=True))
    await asyncio.wait_for(resolved.wait(), timeout=5)

    async def activate():
        policy = {
            "version": 1,
            "domain": {
                "principals": [{"id": "agent:1", "class": "agent", "attributes": {}}],
                "actions": ["read"],
                "memory_classes": ["trace"],
                "label_sets": [[]],
                "purposes": ["default"],
            },
            "rules": [],
        }
        async with session_scope(session_factory, org_id=org) as session:
            activation_started.set()
            row = await policies._locked(session, org)
            activated.set()
            row.generation, row.policy = 1, policy
            row.activation = {"record": {"generation": 1, "policy_hash": fp.digest(policy)}}

    activation = asyncio.create_task(activate())
    await activation_started.wait()
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(activated.wait(), timeout=0.1)
    finally:
        release.set()
        await inflight
        await activation
    assert activated.is_set()
    with pytest.raises(contract.LearningUnavailable):
        await assign(session_factory, org, source, "after-revocation")


async def test_learning_migration_forces_tenant_rls_for_restricted_runtime(session_factory):
    import uuid

    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from hub.tests.test_memory_vector_events import _migration

    engine = session_factory.kw["bind"]
    async with engine.connect() as connection:
        permitted = await connection.scalar(
            text("SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname=current_user")
        )
    if not permitted:
        pytest.skip("migration enforcement needs a disposable PostgreSQL role administrator")
    suffix = uuid.uuid4().hex
    schema, reader = "learning_migration_" + suffix, "learning_reader_" + suffix
    org, other = str(uuid.uuid4()), str(uuid.uuid4())
    tables = ("learning_experiments", "learning_occasions", "learning_decisions", "learning_work")
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            await connection.execute(text("CREATE TABLE organizations(id UUID PRIMARY KEY)"))
            await connection.execute(text("CREATE TABLE holdout_observations(id UUID PRIMARY KEY)"))
            await connection.execute(
                text("INSERT INTO organizations VALUES (:org),(:other)"), {"org": org, "other": other}
            )
            await connection.run_sync(_migration, "ff69cbd3e507_measured_learning.py")
            await connection.execute(text(f'CREATE ROLE "{reader}" NOSUPERUSER NOBYPASSRLS'))
            await connection.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO "{reader}"'))
            for table in tables:
                await connection.execute(text(f'GRANT SELECT,INSERT,UPDATE,DELETE ON {table} TO "{reader}"'))
            for tenant in (org, other):
                for statement in (
                    "INSERT INTO learning_experiments(org_id,id,config,active,created_at) "
                    "VALUES (:org,'e','{}',true,now())",
                    "INSERT INTO learning_occasions(org_id,experiment_id,principal,occasion_id,evidence,created_at) "
                    "VALUES (:org,'e','agent','o','{}',now())",
                    "INSERT INTO learning_decisions(org_id,experiment_id,revision,decision,created_at) "
                    "VALUES (:org,'e',0,'{}',now())",
                    "INSERT INTO learning_work(org_id,experiment_id,pending,generation) VALUES (:org,'e',true,1)",
                ):
                    await connection.execute(text(statement), {"org": tenant})
            forced = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_class WHERE relnamespace=current_schema()::regnamespace "
                    "AND relname LIKE 'learning_%' AND relrowsecurity AND relforcerowsecurity"
                )
            )
            assert forced == 4
            await connection.execute(text(f'SET LOCAL ROLE "{reader}"'))
            await connection.execute(text("SELECT set_config('app.org_id',:org,true)"), {"org": org})
            for table in tables:
                assert (await connection.execute(text(f"SELECT org_id::text FROM {table}"))).scalars().all() == [org]
            with pytest.raises(DBAPIError, match="row-level security"):
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            "INSERT INTO learning_decisions(org_id,experiment_id,revision,decision,created_at) "
                            "VALUES (:other,'e',999,'{}',now())"
                        ),
                        {"other": other},
                    )
            await connection.execute(text("SELECT set_config('app.org_id','',true)"))
            for table in tables[:-1]:
                assert (await connection.execute(text(f"SELECT org_id FROM {table}"))).all() == []
            # Only queue routing metadata supports the trusted unscoped scheduler.
            assert len((await connection.execute(text("SELECT org_id FROM learning_work"))).all()) == 2
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await connection.execute(text(f'DROP ROLE IF EXISTS "{reader}"'))


async def test_numeric_evaluation_offloads_cancels_and_retries_without_partial_commit(
    session_factory, config, monkeypatch
):
    import threading
    import time

    from hub.models import LearningExperiment
    from hub.server import build_mcp_server
    from hub.tests.test_api_key_scopes import payload as mcp_payload
    from unimem.async_workers import BoundedWorkers, WorkerCapacityError

    org, source, experiment = await setup(session_factory)
    assignment = await assign(session_factory, org, source)
    await learning.operate(
        session_factory, org, "agent:1", "delivery", {"occasion_id": "o", "memory_ids": assignment["inject"]}
    )
    await learning.operate(session_factory, org, "agent:1", "outcome", {"occasion_id": "o", "succeeded": True})
    async with session_scope(session_factory) as session:
        issued = await auth.issue_api_key(session, org, scopes=["read", "write", "admin"])
    original_evaluate, original_workers = contract.evaluate, learning.STORE_WORKERS
    workers = BoundedWorkers(workers=1, max_pending=1)
    started, release = threading.Event(), threading.Event()

    def slow_evaluate(*args):
        started.set()
        assert release.wait(timeout=5), "test did not release numeric worker"
        return original_evaluate(*args)

    monkeypatch.setattr(learning, "STORE_WORKERS", workers)
    monkeypatch.setattr(contract, "evaluate", slow_evaluate)
    began = time.monotonic()
    task = asyncio.create_task(learning.operate(session_factory, org, "operator", "run", admin=True))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 3), timeout=4)
        # An unrelated coroutine progresses while numeric evaluation is blocked.
        await asyncio.wait_for(asyncio.sleep(0), timeout=0.2)
        assert time.monotonic() - began < 2
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with session_scope(session_factory, org_id=org) as session:
            exp = await session.get(LearningExperiment, (org, experiment))
            work = await session.get(LearningWork, (org, experiment))
            assert exp.checkpoint is None and exp.report is None
            assert work.pending
            assert len((await session.execute(select(LearningDecision))).scalars().all()) == 1
        # Cancellation retains the underlying thread's capacity slot until done.
        with pytest.raises(WorkerCapacityError):
            await learning.operate(session_factory, org, "operator", "run", admin=True)
        app = Starlette()
        rest.add_rest_routes(app, session_factory, config=config, rate_limiter=make_rate_limiter(config))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/v1/learning/run", headers={"X-API-Key": issued.raw_key}, json={})
            assert response.status_code == 503 and response.headers["Retry-After"] == "1"
            assert response.json()["error"] == "learning_busy"
        mcp = build_mcp_server(config, session_factory, make_rate_limiter(config))
        org_token, scopes_token = auth.current_org_id.set(org), auth.current_scopes.set(("read", "write", "admin"))
        actor_token = auth.current_actor.set("capacity-test")
        try:
            result = mcp_payload(await mcp.call_tool("learning_run", {}))
            assert result["status"] == 503 and result["retry_after"] == 1
        finally:
            auth.current_actor.reset(actor_token)
            auth.current_scopes.reset(scopes_token)
            auth.current_org_id.reset(org_token)
        attempted, stop = asyncio.Event(), asyncio.Event()
        original_complete = learning.complete_work

        async def completed(*args):
            try:
                return await original_complete(*args)
            finally:
                attempted.set()

        monkeypatch.setattr(learning, "complete_work", completed)
        worker = asyncio.create_task(learning.run_worker(session_factory, stop_event=stop, interval_seconds=0.01))
        try:
            await asyncio.wait_for(attempted.wait(), timeout=3)
        finally:
            stop.set()
            await asyncio.wait_for(worker, timeout=3)
        async with session_scope(session_factory, org_id=org) as session:
            work = await session.get(LearningWork, (org, experiment))
            assert work.pending and work.failure == "worker_capacity" and work.lease_token is None
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await workers.close()
        monkeypatch.setattr(learning, "STORE_WORKERS", original_workers)
    # Retrying after capacity is available commits one complete checkpoint.
    await learning.operate(session_factory, org, "operator", "run", admin=True)
    async with session_scope(session_factory, org_id=org) as session:
        exp = await session.get(LearningExperiment, (org, experiment))
        work = await session.get(LearningWork, (org, experiment))
        assert exp.checkpoint is not None and not work.pending and work.failure is None
