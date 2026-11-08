"""The hosted learning console uses the same authority and evidence as REST."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
from sqlalchemy import select, update
from starlette.applications import Starlette

from hub import auth, console, learning
from hub.db import make_engine, make_session_factory, session_scope
from hub.models import ApiKey, AuditLogEntry, LearningExperiment, Organization, Trace
from unimem import learning_contract as contract
from unimem.async_workers import WorkerCapacityError

pytestmark = pytest.mark.asyncio


def app_for(session_factory, config):
    app = Starlette()
    console.add_console_routes(app, session_factory, console_secret="console-learning-test-secret", config=config)
    return app


async def identity(session_factory, granted=None):
    async with session_scope(session_factory) as session:
        org = Organization(name="Learning operators")
        session.add(org)
        await session.flush()
        key = await auth.issue_api_key(session, org.id, scopes=granted)
        return org.id, key


def client_for(session_factory, config):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(session_factory, config)), base_url="http://test"
    )


async def sign_in(client, key):
    response = await client.post("/app/signin", data={"api_key": key.raw_key})
    assert response.status_code == 303


def configuration(**extra):
    return {"mode": "observe", "holdout_rate": "0.5", "min_arm": "10", "confirm": "yes", **extra}


async def test_unauthenticated_learning_reads_and_writes_require_signin(session_factory, config):
    async with client_for(session_factory, config) as client:
        for method, path in (("GET", ""), ("POST", "/configure"), ("POST", "/run"), ("POST", "/rollback")):
            response = await client.request(method, "/app/learning" + path)
            assert response.status_code == 303
            assert response.headers["location"] == "/app/signin"


async def test_unconfigured_learning_explains_first_experiment(session_factory, config):
    _, key = await identity(session_factory)
    async with client_for(session_factory, config) as client:
        await sign_in(client, key)
        response = await client.get("/app/learning")
    assert response.status_code == 200
    assert "Not configured" in response.text
    assert "Start new experiment" in response.text
    assert "Evaluate evidence" not in response.text
    assert 'href="/app/learning"' in response.text
    assert "aria-current=page" in response.text


async def test_console_configuration_preserves_utility_and_creates_new_experiment(session_factory, config):
    org, key = await identity(session_factory)
    utility = {"terms": {"task_score": {"weight": 1, "low": 0, "high": 10, "better": "higher"}}}
    async with client_for(session_factory, config) as client:
        await sign_in(client, key)
        response = await client.post("/app/learning/configure", data=configuration(utility=json.dumps(utility)))
        assert response.status_code == 303
        page = await client.get(response.headers["location"])
        assert "New experiment started" in page.text
        assert "task_score" in page.text
        assert "Organisation evidence" in page.text
        first = await learning.operate(session_factory, org, "key:" + key.key_id, "status", admin=True)
        assert first["config"]["utility"]["terms"]["task_score"]["high"] == 10
        await client.post("/app/learning/configure", data=configuration(mode="adaptive", utility=json.dumps(utility)))
        second = await learning.operate(session_factory, org, "key:" + key.key_id, "status", admin=True)
        assert second["experiment_id"] != first["experiment_id"]
        assert second["config_revision"] == first["config_revision"] + 1
        assert second["config"]["utility"] == first["config"]["utility"]


@pytest.mark.parametrize(
    "fields, message",
    [
        ({"confirm": ""}, "Confirm that saving"),
        ({"holdout_rate": "nan"}, "holdout_rate"),
        ({"min_arm": "9"}, "min_arm"),
        ({"min_arm": "10.5"}, "whole-number minimum"),
        ({"utility": "{broken"}, "utility JSON"),
        ({"utility": "[]"}, "utility needs"),
        ({"unexpected": "tenant:someone-else"}, "Unexpected or repeated"),
    ],
)
async def test_invalid_configuration_cannot_mutate_experiment(session_factory, config, fields, message):
    org, key = await identity(session_factory)
    async with client_for(session_factory, config) as client:
        await sign_in(client, key)
        response = await client.post("/app/learning/configure", data=configuration(**fields))
    assert response.status_code == 400
    assert message in response.text
    async with session_scope(session_factory, org_id=org) as session:
        assert not (await session.execute(select(LearningExperiment))).scalars().all()


async def test_learning_admin_actions_require_scope_and_same_origin(session_factory, config):
    org, owner = await identity(session_factory)
    await learning.operate(session_factory, org, "key:" + owner.key_id, "configure", {"mode": "observe"}, admin=True)
    async with session_scope(session_factory) as session:
        reader = await auth.issue_api_key(session, org, scopes=["read"])
    async with client_for(session_factory, config) as client:
        await sign_in(client, reader)
        page = await client.get("/app/learning")
        assert "Read access" in page.text
        assert "Start new experiment" not in page.text
        assert "Restore policy" not in page.text
        for operation in ("configure", "run", "rollback"):
            assert (await client.post("/app/learning/" + operation, data=configuration())).status_code == 403
        await sign_in(client, owner)
        for operation in ("configure", "run", "rollback"):
            response = await client.post(
                "/app/learning/" + operation, headers={"Origin": "https://evil.test"}, data=configuration()
            )
            assert response.status_code == 403
        assert (await client.get("/app/learning/configure")).status_code == 405


async def test_evaluate_and_rollback_preserve_evidence_and_key_principal(session_factory, config):
    org, key = await identity(session_factory)
    scope = {"tenant": org}
    configured = await learning.operate(
        session_factory, org, "key:" + key.key_id, "configure", {"mode": "adaptive"}, admin=True
    )
    experiment = configured["experiment_id"]
    async with session_scope(session_factory, org_id=org) as session:
        trace = Trace(
            org_id=org,
            title="source",
            context_text="task",
            solution_text="retained source",
            tags=[],
            agent_type="robot",
        )
        session.add(trace)
        await session.flush()
        source = trace.id
    await learning.operate(
        session_factory,
        org,
        "key:" + key.key_id,
        "assign",
        {"occasion_id": "first", "memory_ids": [source]},
        scope=scope,
    )
    await learning.operate(
        session_factory,
        org,
        "other-principal",
        "assign",
        {"occasion_id": "second", "memory_ids": [source]},
        scope=scope,
    )
    async with client_for(session_factory, config) as client:
        await sign_in(client, key)
        response = await client.post("/app/learning/run", data={"experiment_id": experiment})
        assert response.status_code == 303
        page = await client.get(response.headers["location"])
        assert "Delivery receipts are missing" in page.text
        assert "waiting for outcomes" in page.text
        assert "Organisation evidence" in page.text
        assert "This key’s occasions" in page.text
        # The HTML session resolves the REST key UUID, so only its own occasion
        # is counted while the administrator's evaluation includes both actors.
        status = await learning.operate(session_factory, org, "key:" + key.key_id, "status", scope=scope, admin=True)
        assert status["occasions"] == 1
        assert status["report"]["evidence"] == 2
        response = await client.post(
            "/app/learning/rollback",
            data={"experiment_id": experiment, "decision_revision": "0", "reason": "operator reset"},
        )
        assert response.status_code == 303
        page = await client.get(response.headers["location"])
        assert "Policy restored as a new revision" in page.text
    status = await learning.operate(session_factory, org, "key:" + key.key_id, "status", scope=scope, admin=True)
    assert status["decision_revision"] == 1
    assert status["occasions"] == 1
    async with session_scope(session_factory, org_id=org) as session:
        actions = (await session.execute(select(AuditLogEntry.action))).scalars().all()
    assert {"learning.configure", "learning.run", "learning.rollback"} <= set(actions)


async def test_other_organisation_experiment_cannot_be_targeted(session_factory, config):
    org, key = await identity(session_factory)
    other, _ = await identity(session_factory)
    configured = await learning.operate(session_factory, other, "other", "configure", {"mode": "observe"}, admin=True)
    async with client_for(session_factory, config) as client:
        await sign_in(client, key)
        for operation in ("run", "rollback"):
            fields = {"experiment_id": configured["experiment_id"]}
            if operation == "rollback":
                fields["reason"] = "cross-tenant attempt"
            response = await client.post("/app/learning/" + operation, data=fields)
            assert response.status_code == 404
            assert configured["experiment_id"] not in response.text
    with pytest.raises(contract.LearningError):
        await learning.operate(session_factory, org, "key:" + key.key_id, "status")


async def test_busy_evaluation_is_recoverable_without_diagnostic_leak(session_factory, config, monkeypatch):
    org, key = await identity(session_factory)
    experiment = await learning.operate(
        session_factory, org, "key:" + key.key_id, "configure", {"mode": "observe"}, admin=True
    )
    operate = learning.operate

    async def busy(*args, **kwargs):
        if args[3] == "run":
            raise WorkerCapacityError("private backend diagnostic")
        return await operate(*args, **kwargs)

    monkeypatch.setattr(learning, "operate", busy)
    async with client_for(session_factory, config) as client:
        await sign_in(client, key)
        response = await client.post("/app/learning/run", data={"experiment_id": experiment["experiment_id"]})
    assert response.status_code == 503
    assert "Retry shortly" in response.text
    assert "private backend diagnostic" not in response.text


async def test_rendering_escapes_drafts_and_hides_worker_diagnostics():
    markup = '<img src=x onerror="alert(1)">'
    body = console._render_learning(
        {"config": {"mode": "observe"}, "failures": [markup], "report": {"blocked": [markup]}},
        True,
        error=markup,
        draft={"utility": "</textarea>" + markup},
    )
    assert markup not in body
    assert "&lt;img" in body
    assert "&lt;/textarea&gt;" in body
    assert "last background evaluation failed" in body


async def test_existing_learning_session_loses_admin_access_immediately(session_factory, config):
    org, key = await identity(session_factory)
    async with client_for(session_factory, config) as client:
        await sign_in(client, key)
        async with session_scope(session_factory, org_id=org) as session:
            await session.execute(update(ApiKey).where(ApiKey.id == key.key_id).values(scopes=["read"]))
        response = await client.post("/app/learning/configure", data=configuration())
        assert response.status_code == 403
        assert "Start new experiment" not in response.text
        assert (await client.get("/app/learning")).status_code == 200
        async with session_scope(session_factory, org_id=org) as session:
            await session.execute(update(ApiKey).where(ApiKey.id == key.key_id).values(scopes=[]))
        assert (await client.get("/app/learning")).status_code == 303


async def test_readers_do_not_receive_another_principals_learning_evidence(session_factory, config):
    org, owner = await identity(session_factory)
    await learning.operate(session_factory, org, "key:" + owner.key_id, "configure", {"mode": "observe"}, admin=True)
    async with session_scope(session_factory, org_id=org) as session:
        trace = Trace(
            org_id=org, title="private cohort", context_text="task", solution_text="source", tags=[], agent_type="llm"
        )
        session.add(trace)
        await session.flush()
        source = trace.id
        reader = await auth.issue_api_key(session, org, scopes=["read"])
    await learning.operate(
        session_factory,
        org,
        "key:" + owner.key_id,
        "assign",
        {"occasion_id": "private-occasion", "memory_ids": [source]},
        scope={"tenant": org},
    )
    await learning.operate(session_factory, org, "key:" + owner.key_id, "run", admin=True)
    async with client_for(session_factory, config) as client:
        await sign_in(client, reader)
        page = await client.get("/app/learning")
    assert page.status_code == 200
    assert "Visible evidence" in page.text
    assert "Organisation evidence" not in page.text
    assert source not in page.text
    assert "private-occasion" not in page.text
    assert "Delivery receipts are missing" not in page.text


async def test_rolled_back_policy_is_distinct_from_evaluation_recommendation():
    markup = "<svg onload=alert(1)>"
    body = console._render_learning(
        {
            "config": {"mode": "adaptive"},
            "policy": {},
            "report": {
                "evidence": 50,
                "decisions": [
                    {
                        "key": "cohort",
                        "id": markup,
                        "revision": 1,
                        "task_context": "robot",
                        "verdict": "HURTS",
                        "effect": -0.5,
                        "ci_low": -0.8,
                        "ci_high": -0.2,
                        "disabled": True,
                        "rate": 0.5,
                    }
                ],
            },
        },
        True,
    )
    assert "Hurts" in body
    assert "-50.0%" in body
    assert "-80.0% to -20.0%" in body
    assert "Experiment holdout" in body
    assert "Disabled" not in body
    assert markup not in body
    assert "&lt;svg" in body


async def test_live_learning_console_browser_flow(session_factory, config):
    """Optional browser contract against real HTTP, real sessions, and PostgreSQL."""
    browser_api = pytest.importorskip("playwright.async_api")
    uvicorn = pytest.importorskip("uvicorn")
    _, key = await identity(session_factory)
    # Browser HTTP runs on a different event loop. NullPool avoids sharing a
    # live asyncpg connection across the pytest loop and the server thread.
    engine = make_engine(config, pooled=False)
    factory = make_session_factory(engine)
    with socket.socket() as address:
        address.bind(("127.0.0.1", 0))
        port = address.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app_for(factory, config), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    async with browser_api.async_playwright() as runtime:
        try:
            browser = await runtime.chromium.launch(headless=True)
        except browser_api.Error as exc:
            if "Executable doesn't exist" in str(exc):
                await engine.dispose()
                pytest.skip("Install Playwright Chromium to verify the hosted browser flow")
            raise
        try:
            thread.start()
            deadline = time.monotonic() + 5
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert server.started
            page = await browser.new_page(viewport={"width": 390, "height": 844}, reduced_motion="reduce")
            failures = []
            page.on("pageerror", lambda error: failures.append(str(error)))
            await page.goto(f"http://127.0.0.1:{port}/app/learning")
            await page.get_by_label("API key", exact=True).fill(key.raw_key)
            await page.get_by_role("button", name="Sign in", exact=True).click()
            await page.get_by_role("link", name="Learning", exact=True).click()
            await page.get_by_label("Mode", exact=True).select_option("adaptive")
            utility = '{"terms":{"accuracy":{"weight":1,"low":0,"high":1,"better":"higher"}}}'
            await page.get_by_label("Metrics utility (JSON, optional)", exact=True).fill(utility)
            await page.get_by_label("I understand this starts a new experiment.", exact=True).check()
            await page.get_by_role("button", name="Start new experiment", exact=True).click()
            await browser_api.expect(page.get_by_role("status")).to_contain_text("New experiment started")
            await browser_api.expect(page.get_by_label("Metrics utility (JSON, optional)")).to_contain_text("accuracy")
            await page.get_by_role("button", name="Evaluate evidence", exact=True).click()
            await browser_api.expect(page.get_by_role("status")).to_contain_text("Evaluation completed")
            await page.get_by_label("Revision to restore (optional)", exact=True).fill("0")
            await page.get_by_label("Reason for rollback", exact=True).fill("Browser contract")
            await page.get_by_role("button", name="Restore policy", exact=True).click()
            await browser_api.expect(page.get_by_role("status")).to_contain_text("Policy restored")
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.locator("[data-theme-toggle]").click()
            await page.locator("[data-theme-toggle]").click()
            await browser_api.expect(page.locator("html")).to_have_attribute("data-theme", "dark")
            await page.get_by_label("Reason for rollback", exact=True).focus()
            await page.keyboard.press("Tab")
            await browser_api.expect(page.get_by_role("button", name="Restore policy", exact=True)).to_be_focused()
            assert not failures
        finally:
            server.should_exit = True
            if thread.ident is not None:
                await asyncio.to_thread(thread.join, 5)
                assert not thread.is_alive()
            await browser.close()
            await engine.dispose()
