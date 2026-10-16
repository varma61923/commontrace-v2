"""UniMem signed aggregate ingestion through the authenticated Hub boundary."""
from __future__ import annotations

import copy
import random
from dataclasses import replace

import pytest
from sqlalchemy import select

from commontrace import frontier_protocol as fp
from commontrace import memory_authority, pooled_io
from hub import scopes
from hub.db import session_scope
from hub.models import PooledContribution
from hub.tests.test_rest import _app, _client, _key, _org_with_key

pytestmark = pytest.mark.asyncio
PATH = "/v1/evidence/pool/contributions"


def provision(root, org):
    with memory_authority.writer("contributor", "operator"):
        identity = pooled_io.issue_identity(root, org)
        rng = random.Random(29)
        rows = [{"occasion_id": str(i), "injected": bool(i % 2), "rate": .5,
                 "outcome": rng.random() < .5} for i in range(40)]
        bundle = pooled_io.contribute(root, org, fp.digest("public procedure"), rows, release_id="release")
    card = {**identity, "group": "owner", "stake_cents": 100, "expires_at": "2099-01-01T00:00:00+00:00"}
    return card, bundle["signed"]


async def test_authenticated_signature_replay_conflict_and_cross_tenant(session_factory, config, tmp_path):
    org, key = await _org_with_key(session_factory)
    other, other_key = await _org_with_key(session_factory)
    _, reader = await _org_with_key(session_factory, scopes=[scopes.SCOPE_READ])
    card, signed = provision(str(tmp_path), org)
    cfg = replace(config, ledger_signing_key="test-ledger-key-not-for-production", pool_contributors=(card,))
    async with _client(_app(session_factory, cfg)) as client:
        assert (await client.post(PATH, json={"signed": signed})).status_code == 401
        assert (await client.post(PATH, headers=_key(reader), json={"signed": signed})).status_code == 403
        assert (await client.post(PATH, headers=_key(other_key), json={"signed": signed})).status_code == 403
        response = await client.post(PATH, headers=_key(key), json={"signed": signed})
        assert response.status_code == 201, response.text
        first = response.json()
        repeated = await client.post(PATH, headers=_key(key), json={"signed": signed})
        assert repeated.status_code == 200 and repeated.json()["anchor"] == first["anchor"]
        forged = copy.deepcopy(signed)
        forged["record"]["contribution"]["tenant"] = other
        assert (await client.post(PATH, headers=_key(key), json={"signed": forged})).status_code == 403
        # A valid signature cannot overwrite the same release with a new body.
        changed = copy.deepcopy(signed["record"])
        changed["contribution"]["e_value"] = 2
        with memory_authority.writer("contributor", "operator"):
            conflict = fp.sign(str(tmp_path), changed)
        assert (await client.post(PATH, headers=_key(key), json={"signed": conflict})).status_code == 409
        expired = copy.deepcopy(changed)
        expired["expires_at"] = "2000-01-01T00:00:00+00:00"
        with memory_authority.writer("contributor", "operator"):
            expired = fp.sign(str(tmp_path), expired)
        assert (await client.post(PATH, headers=_key(key), json={"signed": expired})).status_code == 403
        for bad in ({}, {"signed": []}, {"signed": {"record": []}}, {"signed": signed, "org_id": other}):
            assert (await client.post(PATH, headers=_key(key), json=bad)).status_code == 400
        for raw in (b'{"signed":NaN}', b" "*65537):
            assert (await client.post(PATH, headers=_key(key), content=raw)).status_code == 400
    async with session_scope(session_factory, org_id=org) as session:
        query = select(PooledContribution).where(PooledContribution.org_id == org)
        rows = (await session.execute(query)).scalars().all()
        assert len(rows) == 1
    async with session_scope(session_factory, org_id=other) as session:
        assert (await session.execute(select(PooledContribution).where(
            PooledContribution.org_id == other))).scalars().all() == []


async def test_feature_requires_configured_registry_and_signer(session_factory, config, tmp_path):
    org, key = await _org_with_key(session_factory)
    _, signed = provision(str(tmp_path), org)
    for cfg, status in ((config, 503), (replace(config, ledger_signing_key="test-ledger-key"), 403)):
        async with _client(_app(session_factory, cfg)) as client:
            assert (await client.post(PATH, headers=_key(key), json={"signed": signed})).status_code == status
