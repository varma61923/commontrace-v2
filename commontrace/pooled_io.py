"""UniMem signed pooled contributions and immutable per-contributor privacy budgets.

I/O is separate from pooled_evidence's statistical calculations. External keys
are operator-pinned Ed25519 public keys. A verified signature authenticates the
contributor, not the truth of its aggregate statistics or its independence.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from commontrace import _jsonl, memory_authority, pooled_evidence
from commontrace import frontier_protocol as fp
from commontrace import nextwave_store as store
from commontrace import transparency_log as log


def register(root: str, tenant: str, *, principal: str, public_key: str, group: str,
             stake_cents: int, expires_at: str) -> dict[str, Any]:
    store.operator()
    fp.text(tenant, "contributor", 128)
    fp.text(principal, "principal", 128)
    fp.text(group, "operator-established ownership group", 128)
    fp.count(stake_cents, 1, 100_000_000)
    expiry = fp.moment(expires_at)
    if expiry <= fp.now():
        raise ValueError("contributor registration has expired")
    try:
        key = bytes.fromhex(public_key)
    except (ValueError, TypeError):
        raise ValueError("contributor public key must be a 32-byte Ed25519 key") from None
    if len(key) != 32:
        raise ValueError("contributor public key must be a 32-byte Ed25519 key")
    body = {"tenant": tenant, "principal": principal, "public_key": key.hex(),
            "organization": "local-store", "group": group, "stake_cents": stake_cents, "expires_at": expiry}
    # Identity, ownership and stake are immutable: splitting aliases does not
    # create a second independent study or increase an established group's stake.
    store.put(root, "pool-contributors", tenant, body, immutable=True)
    return body


def configure_privacy(root: str, tenant: str, *, epsilon: float, delta: float) -> dict[str, Any]:
    store.operator()
    fp.text(tenant)
    fp.number(epsilon, .001, 100)
    fp.number(delta, 1e-12, .1)
    value = {"tenant": tenant, "epsilon": epsilon, "delta": delta}
    store.put(root, "pool-budgets", tenant, value, immutable=True)
    return value


def contribute(root: str, tenant: str, unit_hash: str, rows: Sequence[dict[str, Any]], *, release_id: str,
               epsilon: float | None = None, delta: float | None = None, propensity_floor: float = .05,
               nuisance: Sequence[float] = (.5, .5), expires_seconds: int = 86400) -> dict[str, Any]:
    store.operator()
    fp.count(expires_seconds, 60, 7 * 86400)
    if len(nuisance) != 2:
        raise ValueError("two frozen public nuisance predictions required")
    design = {"tenant": tenant, "unit_hash": unit_hash, "release_id": release_id,
              "epsilon": epsilon, "delta": delta, "floor": propensity_floor, "nuisance": list(nuisance),
              "input_digest": fp.digest(list(rows))}
    identity = fp.digest({"tenant": tenant, "release_id": release_id})
    with _jsonl.locked(fp.filename(root, store.journal_name("pool-releases"))):
        prior = store.latest(root, "pool-releases").get(identity)
        if prior:
            old = store.get(root, "pool-releases", identity)
            if old["design_hash"] != fp.digest(design):
                raise ValueError("release identity is already bound to different inputs or privacy terms")
            return dict(old["contribution"])
        private = epsilon is not None or delta is not None
        if private:
            budget = store.get(root, "pool-budgets", tenant)
            if epsilon is None or delta is None:
                raise ValueError("privacy release requires epsilon and delta")
            spent = [r for r in store.events(root, "pool-privacy") if r["tenant"] == tenant]
            if sum(r["epsilon"] for r in spent) + epsilon > budget["epsilon"] + 1e-12 \
                    or sum(r["delta"] for r in spent) + delta > budget["delta"] + 1e-15:
                raise PermissionError("UniMem pooled-evidence privacy budget exhausted")
        result = pooled_evidence.summarize(tenant, unit_hash, rows, release_id=release_id,
            propensity_floor=propensity_floor, nuisance=(nuisance[0], nuisance[1]), epsilon=epsilon, delta=delta)
        if private:
            # Charge before materializing a release. A crash can consume budget
            # without releasing data, but can never release uncharged data.
            store.event(root, "pool-privacy", identity, {"tenant": tenant, "epsilon": epsilon, "delta": delta})
        expiry = (datetime.fromisoformat(fp.now()) + timedelta(seconds=expires_seconds)).isoformat()
        signed = fp.sign(root, {"type": "unimem.pooled-contribution", "version": 1,
            "contribution": result, "issued_at": fp.now(), "expires_at": expiry})
        anchor = log.append(root, "unimem-pooled-contribution-v1", {"digest": fp.digest(signed)})
        bundle = {"signed": signed, "anchor": anchor}
        store.put(root, "pool-releases", identity, {"design_hash": fp.digest(design), "contribution": bundle},
                  immutable=True)
        return bundle


def verify(signed: dict[str, Any], *, card: dict[str, Any]) -> dict[str, Any]:
    return pooled_evidence.verify_contribution(signed, card=card)


def accept(root: str, bundle: dict[str, Any]) -> dict[str, Any]:
    store.operator()
    signed = bundle["signed"]
    tenant = fp.text(signed["record"]["contribution"]["tenant"])
    card = store.get(root, "pool-contributors", tenant)
    body = verify(signed, card=card)
    identity = fp.digest({"tenant": tenant, "release_id": body["release_id"]})
    record = {"tenant": tenant, "group": card["group"], "signed": signed,
              "unit_hash": body["unit_hash"], "release_id": body["release_id"]}
    store.put(root, "pool-accepted", identity, record, immutable=True)
    return {"accepted": True, "digest": identity, "unit_hash": body["unit_hash"], "classification": "estimate",
            "assumptions": ["operator-pinned contributor identity; statistical inputs remain contributor assertions"]}


def report(root: str, unit_hash: str, *, independent: bool = False, alpha: float = .05) -> dict[str, Any]:
    pooled_evidence.fingerprint(unit_hash)
    contributions, groups = [], {}
    for record in store.list_objects(root, "pool-accepted"):
        if record["unit_hash"] != unit_hash:
            continue
        card = store.get(root, "pool-contributors", record["tenant"])
        contributions.append(verify(record["signed"], card=card))
        groups[card["tenant"]] = card["group"]
    return pooled_evidence.pool(contributions, groups=groups, independent=independent, alpha=alpha)


def issue_identity(root: str, tenant: str) -> dict[str, Any]:
    """An operator provisions the local identity before signing a contribution."""
    store.operator()
    memory_authority.configure_signing(root, algorithm="ed25519")
    return {"tenant": fp.text(tenant), "public_key": memory_authority.signing_config(root)["public_key"],
            "principal": memory_authority.WRITER.get()[0], "organization": "local-store"}
