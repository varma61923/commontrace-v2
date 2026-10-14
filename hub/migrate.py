"""Plan, verify and apply Hub schema migrations under a PostgreSQL operator lock.

Run with the migration owner's HUB_DATABASE_URL, never the serving role. Reads
are the default; upgrade/rollback require --apply. A rollback additionally needs
an explicit acknowledgement of data loss and a separately restore-tested backup.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import Connection

from hub.config import HubConfig
from hub.db import make_engine

# Stable signed-bigint lock key; only migration operators use this namespace.
_MIGRATION_LOCK = 0x4354524143454D


class MigrationError(ValueError):
    """A safe operator-facing failure, without connection URLs or database text."""


def alembic_config() -> Config:
    path = Path(__file__).resolve().parent
    cfg = Config(str(path / "alembic.ini"), stdout=io.StringIO())
    cfg.set_main_option("script_location", str(path / "alembic"))
    # The application's logging belongs to its caller, not fileConfig(env.py).
    cfg.config_file_name = None
    return cfg


def execute(connection: Connection, operation: str, *, revision: str = "head", apply: bool = False,
            allow_data_loss: bool = False, backup_verified: bool = False) -> dict[str, Any]:
    """Operate on a dedicated connection; Alembic owns revision transactions.

    Concurrent-index migrations require autocommit. Do not wrap this operation
    in a caller-managed transaction or promise whole-chain atomicity.
    """
    if connection.in_transaction():
        raise MigrationError("migration requires a dedicated connection outside an external transaction")
    if operation not in {"status", "plan", "verify", "upgrade", "rollback"}:
        raise MigrationError("unknown migration operation")
    cfg = alembic_config()
    scripts = ScriptDirectory.from_config(cfg)
    heads = tuple(scripts.get_heads())
    current = tuple(MigrationContext.configure(connection).get_current_heads())
    if len(heads) != 1 or len(current) > 1:
        raise MigrationError("migration branches require explicit reconciliation")
    try:
        for value in current:
            scripts.get_revision(value)
        target = None if revision == "base" else scripts.get_revision(revision)
        if target is None and revision != "base":
            raise MigrationError("target revision does not exist")
        target_id = target.revision if target else "base"
        ancestors = {item.revision for item in scripts.iterate_revisions(heads[0], "base")}
        if target_id != "base" and target_id not in ancestors:
            raise MigrationError("target revision is outside the supported migration chain")
        if operation == "rollback":
            if not current:
                raise MigrationError("database has no revision to roll back")
            steps = [item.revision for item in scripts.iterate_revisions(current[0], target_id)]
            if not steps:
                raise MigrationError("rollback target must precede the current revision")
        else:
            steps = [item.revision for item in scripts.iterate_revisions(target_id, current[0] if current else "base")]
            steps.reverse()
    except MigrationError:
        raise
    except Exception as exc:
        raise MigrationError("unknown or incompatible migration revision") from exc
    result: dict[str, Any] = {"operation": operation, "current": list(current), "heads": list(heads),
                              "target": target_id, "steps": steps, "applied": False}
    # Inspection starts an implicit read transaction. End it before Alembic
    # establishes its own transaction and any required autocommit blocks.
    connection.rollback()
    cfg.attributes["connection"] = connection
    if operation == "verify":
        if current != heads:
            raise MigrationError("database is not at the migration head")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                command.check(cfg)
        except Exception as exc:
            raise MigrationError("schema verification failed; inspect drift with Alembic as the migration owner"
                                 ) from exc
        result["verified"] = True
    elif operation in {"upgrade", "rollback"} and apply:
        if operation == "rollback" and not (allow_data_loss and backup_verified):
            raise MigrationError("rollback requires --allow-data-loss and --backup-verified after a restore drill")
        try:
            if operation == "rollback":
                command.downgrade(cfg, target_id)
            else:
                command.upgrade(cfg, target_id)
        except Exception as exc:
            raise MigrationError("migration failed; inspect the current revision before retrying") from exc
        result["applied"] = True
        result["current"] = list(MigrationContext.configure(connection).get_current_heads())
    connection.rollback()
    return result


async def run(operation: str, **kwargs: Any) -> dict[str, Any]:
    engine = make_engine(HubConfig.from_env())
    try:
        async with engine.connect() as connection:
            acquired = await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": _MIGRATION_LOCK})
            await connection.commit()
            if not acquired:
                raise MigrationError("another migration operator holds the schema lock")
            try:
                return await connection.run_sync(lambda sync: execute(sync, operation, **kwargs))
            finally:
                await connection.rollback()
                await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _MIGRATION_LOCK})
                await connection.commit()
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("status", "plan", "verify", "upgrade", "rollback"))
    parser.add_argument("--revision", default="head", help="explicit revision or head/base")
    parser.add_argument("--apply", action="store_true", help="apply using Alembic transaction boundaries")
    parser.add_argument("--allow-data-loss", action="store_true")
    parser.add_argument("--backup-verified", action="store_true",
                        help="attest that a separate backup restore drill succeeded")
    args = parser.parse_args(argv)
    if args.operation == "rollback" and args.revision == "head":
        parser.error("rollback requires an explicit ancestor --revision")
    if args.apply and args.operation not in {"upgrade", "rollback"}:
        parser.error("--apply is only valid for upgrade or rollback")
    if args.operation == "rollback" and args.apply and not (args.allow_data_loss and args.backup_verified):
        parser.error("rollback needs --allow-data-loss and --backup-verified")
    try:
        result = asyncio.run(run(args.operation, revision=args.revision, apply=args.apply,
                                 allow_data_loss=args.allow_data_loss, backup_verified=args.backup_verified))
    except MigrationError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    except Exception as exc:
        print(json.dumps({"error": "migration connection or configuration failed", "error_type": type(exc).__name__}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
