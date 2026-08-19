"""Operator CLI for org/API-key management: `python -m hub.manage <command>`.

    create-org <name>              -> prints the new org's id
    issue-key <org_id>             -> prints the raw key ONCE (see warning below)
    rotate-key <key_id>            -> revokes <key_id>, issues + prints a new raw key for the same org
    revoke-key <key_id>            -> revokes a key immediately
    list-orgs                      -> id, name, created_at

The raw API key is only ever available at issuance/rotation time -- it is
never stored in recoverable form (hub/auth.py hashes it with argon2 before
the row is written) and is not logged. Copy it to wherever the org will
configure their MCP client now; there is no way to retrieve it again later,
only to rotate to a new one.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from hub import auth
from hub.config import HubConfig
from hub.db import make_engine, make_session_factory, session_scope
from hub.models import ApiKey, Organization


async def create_org(name: str) -> None:
    config = HubConfig.from_env()
    session_factory = make_session_factory(make_engine(config))
    async with session_scope(session_factory) as session:
        org = Organization(name=name)
        session.add(org)
        await session.flush()
        print(f"org_id: {org.id}")


async def issue_key(org_id: str) -> None:
    config = HubConfig.from_env()
    session_factory = make_session_factory(make_engine(config))
    async with session_scope(session_factory) as session:
        issued = await auth.issue_api_key(session, org_id)
    print(f"key_id: {issued.key_id}")
    print(f"api_key (shown once, store it now): {issued.raw_key}")


async def rotate_key(key_id: str) -> None:
    config = HubConfig.from_env()
    session_factory = make_session_factory(make_engine(config))
    async with session_scope(session_factory) as session:
        issued = await auth.rotate_api_key(session, key_id)
    print(f"revoked: {key_id}")
    print(f"new key_id: {issued.key_id}")
    print(f"new api_key (shown once, store it now): {issued.raw_key}")


async def revoke_key(key_id: str) -> None:
    config = HubConfig.from_env()
    session_factory = make_session_factory(make_engine(config))
    async with session_scope(session_factory) as session:
        await auth.revoke_api_key(session, key_id)
    print(f"revoked: {key_id}")


async def list_orgs() -> None:
    config = HubConfig.from_env()
    session_factory = make_session_factory(make_engine(config))
    async with session_scope(session_factory) as session:
        orgs = (await session.execute(select(Organization))).scalars().all()
        for org in orgs:
            n_keys = (
                await session.execute(select(ApiKey).where(ApiKey.org_id == org.id, ApiKey.revoked_at.is_(None)))
            ).scalars().all()
            print(f"{org.id}  {org.name!r}  created={org.created_at.isoformat()}  active_keys={len(n_keys)}")


_COMMANDS = {
    "create-org": (create_org, 1),
    "issue-key": (issue_key, 1),
    "rotate-key": (rotate_key, 1),
    "revoke-key": (revoke_key, 1),
    "list-orgs": (list_orgs, 0),
}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in _COMMANDS:
        print(__doc__)
        return 1 if argv else 0

    fn, n_args = _COMMANDS[argv[0]]
    args = argv[1:]
    if len(args) != n_args:
        print(f"error: {argv[0]} takes {n_args} argument(s), got {len(args)}", file=sys.stderr)
        return 2

    asyncio.run(fn(*args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
