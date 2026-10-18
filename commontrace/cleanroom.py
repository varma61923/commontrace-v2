"""Sealed buyer trials, deduplicated royalties and per-querier DP releases.

A sandbox/enclave hook is an interface, not a claim that Python hides plaintext
from its host. Production sealing requires an independently secured seller-side
runner. Royalties require canonical buyer evidence, e-process support and a
verified valuation. Operator-established sellers/stakes are the Sybil boundary.
DP uses clipped fixed-size means and basic sequential epsilon composition;
querier identities must be authenticated to prevent budget resets/collusion.
"""
from __future__ import annotations

import math
import random
from typing import Any, Callable, Protocol, Sequence, cast

from commontrace import _jsonl, evidence, holdout_io, overlap, valuation
from commontrace import frontier_protocol as fp
from commontrace import transparency_log as log


class SealedRunner(Protocol):
    def run(self, handle: str, occasion: str, *, injected: bool) -> bool: ...
    def attest(self, handle: str) -> dict[str, Any]: ...


def listing(root: str, *, seller: str, handle: str, content_digest: str, minhash: list[int],
            stake_cents: int) -> dict[str, Any]:
    fp.text(seller)
    fp.text(handle)
    if len(content_digest) != 64 or any(c not in "0123456789abcdef" for c in content_digest):
        raise ValueError("sealed listing requires a SHA256 content commitment")
    fp.count(len(minhash), 16, 256)
    for x in minhash:
        fp.count(x, 0, 2**64 - 1)
    fp.count(stake_cents, 1, 100_000_000)
    body = {"id": "sealed-" + content_digest[:24], "seller": seller, "handle": handle,
            "content_digest": content_digest, "minhash": minhash, "stake_cents": stake_cents}
    return fp.sign(root, body)


def trial(root: str, offer: dict[str, Any], runner: SealedRunner, occasion: str, *,
          verify_offer: Callable[[dict[str, Any]], bool],
          verify_attestation: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    if not verify_offer(offer):
        raise PermissionError("seller listing signature is not trusted")
    body = offer["record"]
    if not verify_attestation(runner.attest(body["handle"])):
        raise PermissionError("seller-side sandbox attestation was not accepted")
    config = holdout_io.load_config(root)
    if not config.running:
        raise ValueError("sealed trial requires a randomized buyer experiment")
    held = holdout_io.assign_and_log(root, [body["id"]], occasion_id=occasion, rate=config.rate, salt=config.salt,
                                    revisions={body["id"]: body["content_digest"]}, context="sealed-trial")
    injected = body["id"] not in held
    success = runner.run(body["handle"], fp.text(occasion), injected=injected)
    if type(success) is not bool:
        raise ValueError("sealed runner may return only the bounded binary outcome")
    holdout_io.record_outcome(root, occasion, success)
    return {"occasion_id": occasion, "injected": injected, "success": success,
            "content_exposed": False, "listing": body["id"]}


def attested_trial(root: str, offer: dict[str, Any], runner: SealedRunner, occasion: str, *,
                   verify_offer: Callable[[dict[str, Any]], bool],
                   verify_attestation: Callable[[dict[str, Any]], bool], tenant: str, measurement: str,
                   worker_key: str, result_quote: Callable[[dict[str, Any]], dict[str, Any]],
                   verifier: Any, allow_simulated: bool = False) -> dict[str, Any]:
    """Ordinary sealed trial plus a fresh quote bound to its exact result bytes.

    The deployment-controlled worker supplies result_quote using its actual
    hardware report-data API. No Python wrapper can create hardware evidence.
    Failure withholds the attested result; the real canonical outcome remains.
    """
    from commontrace import attested_keys

    attested_keys.admitted(root, measurement)
    result = trial(root, offer, runner, occasion, verify_offer=verify_offer,
                   verify_attestation=verify_attestation)
    challenge = attested_keys.issue(root, tenant, measurement, worker_key,
        operation="sealed-trial", payload_digest=fp.digest(result))
    evidence = result_quote(challenge)
    appraisal = attested_keys.accept_result(root, challenge, evidence, verifier, allow_simulated=allow_simulated)
    return {"result": result, "result_digest": fp.digest(result), **appraisal}


def royalties(offers: Sequence[dict[str, Any]], values: dict[str, float], budget_cents: int, *,
              duplicate_threshold: float = .8) -> dict[str, Any]:
    fp.count(len(offers), 0, 256)
    fp.count(budget_cents, 0, 100_000_000)
    fp.number(duplicate_threshold, .5, 1)
    groups: list[list[dict[str, Any]]] = []
    for offer in offers:
        fp.text(offer["id"])
        fp.text(offer["seller"])
        fp.count(len(offer["minhash"]), 16, 256)
        for value in offer["minhash"]:
            fp.count(value, 0, 2**64 - 1)
        fp.number(values.get(offer["id"], 0), -1, 1)
        matches = [group for group in groups if any(
            overlap.estimate_jaccard(offer["minhash"], item["minhash"]) >= duplicate_threshold for item in group)]
        merged = [offer]
        for group in matches:
            merged.extend(group)
            groups.remove(group)
        groups.append(merged)
    weights = [max(0, max(values.get(item["id"], 0) for item in group)) for group in groups]
    denominator = sum(weights)
    payouts: dict[str, int] = {}
    clusters = []
    for group, weight in zip(groups, weights):
        reward = int(budget_cents * weight / denominator) if denominator else 0
        # Within a cluster the reward is split by slashable stake, not per seller:
        # copy-cat listings cannot dilute an originator without risking real capital.
        stakes: dict[str, int] = {}
        for item in group:
            stake = fp.count(item.get("stake_cents", 1), 1, 100_000_000, "stake")
            stakes[item["seller"]] = stakes.get(item["seller"], 0) + stake
        total = sum(stakes.values())
        for seller, stake in sorted(stakes.items()):
            payouts[seller] = payouts.get(seller, 0) + reward * stake // total
        clusters.append({"listings": sorted({x["id"] for x in group}), "reward_cents": reward,
                         "interventional_value": weight, "stakes": dict(sorted(stakes.items()))})
    return {"payouts": payouts, "clusters": clusters, "paid_cents": sum(payouts.values()),
            "kind": "one reward per near-duplicate cluster, split by slashable stake"}


def overlap_price(base_cents: int, seller_signature: list[int], buyer_signature: list[int]) -> int:
    fp.count(base_cents, 0, 100_000_000)
    return round(base_cents * (1 - overlap.estimate_jaccard(seller_signature, buyer_signature)))


def settle(root: str, escrow: str, offer: dict[str, Any], certificate: dict[str, Any], *,
           amount_cents: int, verify_offer: Callable[[dict[str, Any]], bool], minimum_e: float = 20) -> dict[str, Any]:
    fp.text(escrow)
    fp.count(amount_cents, 0, 100_000_000)
    fp.number(minimum_e, 1, 1e12)
    if not verify_offer(offer) or not valuation.verify(root, certificate, require_attested=True):
        raise PermissionError("escrow requires a trusted offer and ledger-attested valuation certificate")
    identity = offer["record"]["id"]
    certified = certificate["certificate"]["record"]["report"]
    if certified.get("source_revisions", {}).get(identity) != offer["record"]["content_digest"]:
        raise PermissionError("valuation certificate does not cover this sealed content commitment")
    analysis = evidence.analyse(root)
    if analysis.report is None or not getattr(analysis.report, "readable", False):
        raise PermissionError("escrow requires a readable current canonical integrity audit")
    effects = {e.lesson_slug: e for e in analysis.effects}
    effect = effects.get(identity)
    values = certificate["certificate"]["record"]["report"]["values"]
    positive = next((v for v in values if v["memories"] == [identity] and v["interval"][0] > 0), None)
    if effect is None or positive is None or (effect.e_value or 0) < minimum_e or effect.effect <= 0:
        raise PermissionError("escrow requires positive canonical lift and directional e-process support")
    with _jsonl.locked(fp.filename(root, "sealed-escrow.jsonl")):
        prior = [r for r in fp.journal(root, "sealed-escrow.jsonl") if r["escrow"] == escrow]
        if prior:
            return prior[0]
        body = {"escrow": escrow, "listing": identity, "seller": offer["record"]["seller"],
                "amount_cents": amount_cents, "certificate_digest": fp.digest(certificate), "status": "released"}
        signed = fp.append(root, "sealed-escrow.jsonl", body)
        log.append(root, "sealed-escrow-release", signed)
        return cast(dict[str, Any], signed["record"])


def slash(root: str, offer: dict[str, Any], *, verify_offer: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    if not verify_offer(offer):
        raise PermissionError("stake slashing requires the trusted signed offer")
    identity = offer["record"]["id"]
    report = evidence.for_lessons(root)
    measured = report.get("by_lesson", {}).get(identity, {}) if report.get("available") else {}
    if measured.get("verdict") != "HURTS":
        raise PermissionError("stake slashing requires a canonical significant harm verdict")
    return fp.append(root, "sealed-slashes.jsonl", {"listing": identity,
        "stake_cents": offer["record"]["stake_cents"], "reason": "canonical HURTS"})


def private_effect(root: str, querier: str, query: str, effects: Sequence[float], *, epsilon: float,
                   total_budget: float = 1) -> dict[str, Any]:
    fp.text(querier)
    fp.text(query)
    fp.number(epsilon, .001, 10)
    fp.number(total_budget, .001, 10)
    fp.count(len(effects), 1, 10_000)
    for value in effects:
        fp.number(value, -1, 1)
    name = "effect-privacy.jsonl"
    with _jsonl.locked(fp.filename(root, name)):
        history = [r for r in fp.journal(root, name) if r["querier"] == querier]
        # The first configured budget is immutable: callers cannot raise it per query.
        budget = history[0]["total_budget"] if history else total_budget
        prior = next((r for r in history if r["query"] == query), None)
        if prior:
            return prior
        spent = sum(r["epsilon"] for r in history)
        if spent + epsilon > budget + 1e-12:
            raise PermissionError("differential privacy budget exhausted")
        scale = 2 / len(effects) / epsilon
        uniform = random.SystemRandom().uniform(-.5 + 1e-15, .5 - 1e-15)
        noise = -scale * math.copysign(math.log1p(-2 * abs(uniform)), uniform)
        body = {"querier": querier, "query": query, "epsilon": epsilon, "spent": spent + epsilon,
                "total_budget": budget, "effect": sum(effects) / len(effects) + noise,
                "mechanism": "Laplace, replacement adjacency, fixed cohort size, clipped [-1,1] mean"}
        # No raw effect sizes or cohort membership enter the release ledger.
        return cast(dict[str, Any], fp.append(root, name, body)["record"])


class StripeConnect:
    def __init__(self, client: Any, *, enabled: bool = False, root: str | None = None) -> None:
        if enabled and root is None:
            raise ValueError("enabled payments require a trusted local settlement ledger")
        self.client, self.enabled, self.root = client, enabled, root

    def pay(self, settlement: dict[str, Any], account: str) -> Any:
        if not self.enabled or settlement.get("status") != "released":
            raise PermissionError("Stripe Connect is disabled or escrow has not released")
        if self.root is None or settlement not in fp.journal(self.root, "sealed-escrow.jsonl"):
            raise PermissionError("payment requires the exact authenticated escrow ledger release")
        fp.text(account, "Stripe account", 128)
        return self.client.transfers.create(amount=settlement["amount_cents"], currency="usd", destination=account,
            idempotency_key="commontrace-" + fp.digest(settlement["escrow"]))
