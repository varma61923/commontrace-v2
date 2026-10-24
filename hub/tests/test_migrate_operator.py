from __future__ import annotations

import uuid

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import text

from hub import migrate
from hub.db import make_engine


@pytest.mark.asyncio
async def test_webhook_audit_migration_removes_credentials_and_preserves_other_actions(config):
    engine = make_engine(config)
    schema = "privacy_contract_" + uuid.uuid4().hex
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.execute(text(f'SET search_path TO "{schema}"'))
            await connection.commit()

            async def upgrade(revision):
                await connection.run_sync(lambda sync: migrate.execute(
                    sync, "upgrade", revision=revision, apply=True))

            await upgrade("ff47a9b1c3e5")
            for action in ("webhook.add", "webhook.disable", "issue_key"):
                await connection.execute(text(
                    "INSERT INTO audit_log (id, created_at, actor, action, target_type, target_id, summary) "
                    "VALUES (:id, now(), 'test', :action, 'endpoint', 'one', :summary)"
                ), {"id": str(uuid.uuid4()), "action": action,
                    "summary": "unchanged" if action == "issue_key" else "https://user:secret@example.com/token"})
            await connection.commit()
            await upgrade("ff58bac2d4f6")
            rows = dict((await connection.execute(text("SELECT action, summary FROM audit_log"))).all())
            assert rows == {"webhook.add": "Webhook destination redacted",
                            "webhook.disable": "Webhook destination redacted", "issue_key": "unchanged"}
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


@pytest.mark.asyncio
async def test_plan_upgrade_verify_rollback_and_reupgrade_on_real_postgres(config):
    engine = make_engine(config)
    schema = "migration_contract_" + uuid.uuid4().hex
    try:
        async with engine.connect() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.execute(text(f'SET search_path TO "{schema}"'))
            await connection.commit()

            def run(operation, **kwargs):
                return connection.run_sync(lambda sync: migrate.execute(sync, operation, **kwargs))
            initial = await run("plan")
            assert initial["current"] == [] and initial["steps"] and not initial["applied"]
            await run("upgrade", apply=True)
            verified = await run("verify")
            assert verified["verified"] and verified["current"] == verified["heads"]
            await connection.execute(text("CREATE TABLE unexpected_migration_drift (id integer)"))
            await connection.commit()
            with pytest.raises(migrate.MigrationError, match="schema verification"):
                await run("verify")
            await connection.execute(text("DROP TABLE unexpected_migration_drift"))
            await connection.commit()
            scripts = ScriptDirectory.from_config(migrate.alembic_config())
            head = scripts.get_revision(scripts.get_current_head())
            previous = head.down_revision
            assert isinstance(previous, str)
            before = await run("rollback", revision=previous)
            assert before["steps"] == [head.revision] and not before["applied"]
            with pytest.raises(migrate.MigrationError, match="backup"):
                await run("rollback", revision=previous, apply=True)
            result = await run("rollback", revision=previous, apply=True, allow_data_loss=True, backup_verified=True)
            assert result["current"] == [previous]
            with pytest.raises(migrate.MigrationError, match="head"):
                await run("verify")
            await run("upgrade", apply=True)
            assert (await run("verify"))["verified"]
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


@pytest.mark.asyncio
async def test_unknown_revision_and_operator_lock(config, monkeypatch):
    engine = make_engine(config)
    try:
        async with engine.connect() as first, engine.connect() as second:
            assert await first.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": migrate._MIGRATION_LOCK})
            monkeypatch.setattr(migrate.HubConfig, "from_env", lambda: config)
            with pytest.raises(migrate.MigrationError, match="another migration"):
                await migrate.run("status")
            await second.rollback()
            with pytest.raises(migrate.MigrationError, match="revision"):
                await second.run_sync(lambda sync: migrate.execute(sync, "plan", revision="bad-revision"))
            await first.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": migrate._MIGRATION_LOCK})
            await first.commit()
            with pytest.raises(migrate.MigrationError, match="revision"):
                await migrate.run("plan", revision="bad-revision")
            assert await first.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": migrate._MIGRATION_LOCK})
            await first.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": migrate._MIGRATION_LOCK})
    finally:
        await engine.dispose()


def test_cli_connection_errors_never_expose_credentials(monkeypatch, capsys):
    async def fail(*args, **kwargs):
        raise RuntimeError("postgresql://operator:very-private@host/db")

    monkeypatch.setattr(migrate, "run", fail)
    assert migrate.main(["status"]) == 1
    assert "very-private" not in capsys.readouterr().out


def test_rollback_guard_rejects_before_opening_database(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("database must not be contacted")

    monkeypatch.setattr(migrate, "run", forbidden)
    with pytest.raises(SystemExit):
        migrate.main(["rollback", "--revision", "base", "--apply"])
