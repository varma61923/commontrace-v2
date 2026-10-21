"""Interventional value, authenticated exports and bounded warranty credits.

Bernoulli factorial contrasts are a design analogy to Data Banzhaf's maximum
sample reuse estimator (Wang & Jia, AISTATS 2023), not an equivalence claim.
Centered inverse-propensity regressors identify main and pairwise contrasts in
an independently randomized Bernoulli design, including heterogeneous rates.
Intervals use bounded Hoeffding contrasts, union corrected for a fixed family.
They require independent tasks, bounded outcomes and the declared design.
"""
from __future__ import annotations

import math
from dataclasses import asdict
from typing import Any, Sequence, cast

from unimem import _jsonl, conformal_gate
from unimem import frontier_protocol as fp
from unimem import transparency_log as log


def factorial(rows: Sequence[dict[str, Any]], memories: Sequence[str], *,
              propensity_floor: float = .1, alpha: float = .05) -> dict[str, Any]:
    n = fp.count(len(rows), 1, fp.MAX_ROWS)
    ids = sorted({fp.text(x) for x in memories})
    fp.count(len(ids), 1, 16)
    fp.number(propensity_floor, .001, .5)
    fp.number(alpha, 1e-6, .99)
    terms = [(x,) for x in ids] + [(x, y) for i, x in enumerate(ids) for y in ids[i + 1:]]
    totals = {term: 0.0 for term in terms}
    seen = set()
    for row in rows:
        occasion = fp.text(row["occasion_id"])
        if occasion in seen:
            raise ValueError("valuation requires unique independent task outcomes")
        seen.add(occasion)
        y = fp.number(row["outcome"], 0, 1)
        if set(row["included"]) != set(ids) or set(row["probabilities"]) != set(ids):
            raise ValueError("complete independent Bernoulli inclusion vectors required")
        contrasts = {}
        for identity in ids:
            action = row["included"][identity]
            if type(action) is not bool:
                raise ValueError("binary inclusion indicators required")
            p = fp.number(row["probabilities"][identity], propensity_floor, 1 - propensity_floor)
            contrasts[identity] = (int(action) - p) / (p * (1 - p))
        for term in terms:
            totals[term] += y * math.prod(contrasts[x] for x in term)
    values = []
    for term in terms:
        bound = (1 / propensity_floor) ** len(term)
        radius = bound * math.sqrt(2 * math.log(2 * len(terms) / alpha) / n)
        estimate = totals[term] / n
        values.append({"memories": list(term), "estimate": estimate,
                       "interval": [estimate - radius, estimate + radius], "n": n})
    return {"values": values, "n": n, "design_hash": fp.digest({"memories": ids, "floor": propensity_floor,
            "independent_bernoulli": True}), "data_hash": fp.digest(list(rows)), "alpha": alpha,
            "kind": "factorial randomized contrasts; fixed-look finite-sample intervals"}


def certificate(root: str, report: dict[str, Any]) -> dict[str, Any]:
    """Authenticate a report; verified provenance is separate from causal validity.

    A caller-supplied report receives input_attested=False. Settlement requires
    certify_logged(), which reconstructs the canonical randomized evidence.
    """
    fp.wire(report)
    signed = fp.sign(root, {"type": "causal-valuation-v1", "report": report, "input_attested": False})
    return {"certificate": signed, "anchor": log.append(root, "valuation", signed)}


def certify_logged(root: str, memories: Sequence[str], *, propensity_floor: float = .1) -> dict[str, Any]:
    from unimem import allocation, experiment, holdout_io, integrity
    from unimem.commands.experiment_cmd import _load, _observations

    assignments, _, corrupt = _load(root)
    outcomes = holdout_io.read_outcomes(root)
    config = holdout_io.load_config(root)
    assignments = [r for r in assignments if r.salt == config.salt and r.lesson in memories]
    audit = integrity.audit(assignments, schedules=allocation.history(root))
    if corrupt or not audit.readable or allocation.history(root):
        raise PermissionError("unreadable randomized evidence cannot be valued")
    log.head(root)
    assignment_hashes, anchored_outcomes = set(), set()
    for leaf in fp.journal(root, log.LEAVES):
        event = leaf["statement"]["record"]
        if event["event"] == "holdout-assignment":
            assignment_hashes.update(event["statement"]["assignments"].values())
        elif event["event"] == "holdout-outcome":
            anchored_outcomes.add((event["statement"]["occasion_id"], event["statement"]["succeeded"]))
    raw = [r for r in fp.rows(holdout_io.holdout_log_path(root))
           if r["salt"] == config.salt and r["lesson"] in memories]
    if any(fp.digest(r) not in assignment_hashes for r in raw):
        raise PermissionError("valuation assignments require transparency anchors")
    ids = sorted(set(memories))
    grouped: dict[str, dict[str, Any]] = {}
    for row in assignments:
        if row.lesson not in ids:
            continue
        group = grouped.setdefault(row.occasion_id, {"occasion_id": row.occasion_id,
            "included": {}, "probabilities": {}})
        if row.lesson in group["included"]:
            raise ValueError("duplicate valuation assignment")
        group["included"][row.lesson] = row.injected
        group["probabilities"][row.lesson] = 1 - row.rate
    rows = []
    for occasion, group in grouped.items():
        if set(group["included"]) == set(ids) and occasion not in outcomes:
            raise ValueError("resolve every eligible valuation outcome before fixed-horizon certification")
        if set(group["included"]) == set(ids) and occasion in outcomes:
            if (occasion, outcomes[occasion]) not in anchored_outcomes:
                raise PermissionError("valuation outcomes require transparency anchors")
            group["outcome"] = int(outcomes[occasion])
            rows.append(group)
    report = factorial(rows, ids, propensity_floor=propensity_floor)
    report["design_hash"] = fp.digest({"factorial": report["design_hash"], "salt": config.salt,
                                       "started_at": config.started_at, "rate": config.rate})
    revisions: dict[str, str | None] = {}
    for identity in ids:
        versions = {r.revision for r in assignments if r.lesson == identity}
        if len(versions) != 1 or None in versions or "" in versions:
            raise PermissionError("value certificates cannot mix memory content revisions")
        revisions[identity] = next(iter(versions))
    report["source_revisions"] = revisions
    report["canonical_effects"] = [asdict(e) for e in experiment.analyze(_observations(assignments), sequential=True)]
    signed = fp.sign(root, {"type": "causal-valuation-v1", "report": report, "input_attested": True,
                           "integrity": audit.verdict})
    return {"certificate": signed, "anchor": log.append(root, "valuation", signed)}


def verify(root: str, bundle: dict[str, Any], *, require_attested: bool = False) -> bool:
    try:
        signed = bundle["certificate"]
        return (fp.verify(root, signed) and signed["record"]["type"] == "causal-valuation-v1"
                and (not require_attested or signed["record"]["input_attested"])
                and log.verify(root, bundle["anchor"])
                and bundle["anchor"]["statement"]["record"]["statement"] == signed)
    except (KeyError, TypeError, ValueError):
        return False


def warranty_credit(root: str, bundle: dict[str, Any], *, contract: str, memory: str,
                    minimum_lift: float, paid_cents: int) -> dict[str, Any]:
    fp.text(contract)
    fp.number(minimum_lift, .0001, 1)
    fp.count(paid_cents, 0, 100_000_000)
    if not verify(root, bundle, require_attested=True):
        raise PermissionError("warranty requires canonical ledger-attested valuation")
    values = bundle["certificate"]["record"]["report"]["values"]
    value = next((v for v in values if v["memories"] == [memory]), None)
    if value is None:
        raise ValueError("memory absent from valuation certificate")
    # Conservative trigger: the entire interval lies below the contracted floor.
    shortfall = max(0.0, minimum_lift - value["interval"][1])
    cents = min(paid_cents, round(paid_cents * shortfall / minimum_lift))
    identity = fp.digest({"contract": contract, "certificate": bundle["certificate"]["digest"], "memory": memory})
    with _jsonl.locked(fp.filename(root, "warranty-credits.jsonl")):
        history = [r for r in fp.journal(root, "warranty-credits.jsonl") if r["contract"] == contract]
        if any(r["paid_cents"] != paid_cents or r.get("minimum_lift") != minimum_lift for r in history):
            raise ValueError("warranty contract terms are immutable")
        prior = next((r for r in history if r["id"] == identity), None)
        if prior:
            return prior
        cents = min(cents, max(0, paid_cents - sum(r["credit_cents"] for r in history)))
        signed = fp.append(root, "warranty-credits.jsonl", {"id": identity, "contract": contract, "memory": memory,
            "credit_cents": cents, "certificate_digest": bundle["certificate"]["digest"],
            "paid_cents": paid_cents, "minimum_lift": minimum_lift})
        return cast(dict[str, Any], signed["record"])


def underwriting_export(root: str) -> dict[str, Any]:
    from unimem import safety

    body = {"type": "underwriting-evidence-v1", "created_at": fp.now(), "reliability_series": safety.export(root),
            "incidents": fp.journal(root, "incidents.jsonl"),
            "harm_budgets": conformal_gate.current(root),
            "change_certificates": fp.journal(root, "promotion-certificates.jsonl"),
            "warranty_credits": fp.journal(root, "warranty-credits.jsonl"),
            "notice": "Evidence fields for underwriting review; no insurance or regulatory certification."}
    signed = fp.sign(root, body)
    return {"manifest": signed, "anchor": log.append(root, "underwriting-export", signed)}


def verify_export(root: str, bundle: dict[str, Any]) -> bool:
    try:
        return (fp.verify(root, bundle["manifest"]) and log.verify(root, bundle["anchor"])
                and bundle["anchor"]["statement"]["record"]["statement"] == bundle["manifest"])
    except (ValueError, KeyError, TypeError):
        return False


def data_value_export(episodes: Sequence[dict[str, Any]], *, layout: str = "lerobot") -> dict[str, Any]:
    if layout not in ("lerobot", "recap"):
        raise ValueError("layout must be lerobot or recap")
    fp.count(len(episodes), 0, 2000)
    rows = []
    for row in episodes:
        if len(fp.wire(row)) > 8192:
            raise ValueError("episode value metadata exceeds 8 KiB")
        fp.text(row["episode_id"])
        effect = fp.number(row["memory_effect"], -1, 1)
        if type(row["success"]) is not bool:
            raise ValueError("binary observed episode success required")
        segments = row.get("interventions", [])
        fp.count(len(segments), 0, 256)
        for start, end in segments:
            fp.number(start, 0, 1e9)
            fp.number(end, start, 1e9)
        rows.append({"episode_index": row["episode_id"], "next.success": row["success"],
                     "intervention_segments": segments, "memory_effect": effect,
                     "media": row.get("media", []), "value_rank_score": effect})
    rows.sort(key=lambda r: (-r["value_rank_score"], r["episode_index"]))
    return {"layout": layout, "format": "unimem." + layout + "-metadata.v1", "episodes": rows,
            "notice": "Metadata interchange; raw training tensors remain in the robot data plane."}


def export_data(root: str, episodes: Sequence[dict[str, Any]], *, layout: str = "lerobot") -> dict[str, Any]:
    signed = fp.sign(root, data_value_export(episodes, layout=layout))
    return {"manifest": signed, "anchor": log.append(root, "data-value-export", signed)}
