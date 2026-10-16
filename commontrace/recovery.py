"""Embodiment-scoped failure signatures and randomized recovery memories.

Failure explanation fields follow AHA/RoboFAC/FailSafe-style structured reports.
Calibrated scores are empirical estimates from prior labels, not physical safety
guarantees. Recovery lift comes exclusively from the existing randomized evidence
and integrity audit. Sim/real and embodiment boundaries are explicit scopes.
"""
from __future__ import annotations

from typing import Any, Sequence

from commontrace import evidence, experiment, hierarchical, holdout_io, lineage, memory_ifc, safety, search_recipes
from commontrace import frontier_protocol as fp


def signature(failure_type: str, raw_score: float, calibration: Sequence[tuple[float, bool]], *,
               explanation: dict[str, Any], scene: dict[str, Any]) -> dict[str, Any]:
    fp.text(failure_type, "failure type", 128)
    fp.number(raw_score, 0, 1)
    fp.count(len(calibration), 10, 10_000)
    for score, failed in calibration:
        fp.number(score, 0, 1)
        if type(failed) is not bool:
            raise ValueError("calibration labels must be binary")
    fp.wire(explanation)
    fp.wire(scene)
    if len(fp.wire(explanation)) + len(fp.wire(scene)) > 8192:
        raise ValueError("failure explanation and scene exceed 8 KiB")
    neighbors = sorted(calibration, key=lambda row: abs(row[0] - raw_score))[:50]
    probability = (sum(row[1] for row in neighbors) + 1) / (len(neighbors) + 2)
    return {"failure_type": failure_type, "calibrated_score": probability, "calibration_n": len(neighbors),
            "raw_score": raw_score, "explanation": explanation, "scene": scene,
            "calibration_digest": fp.digest(list(calibration)), "kind": "empirically calibrated failure estimate"}


def _scopes(context: dict[str, str]) -> list[str]:
    required = ("site", "robot", "embodiment", "environment")
    if set(context) != set(required) or context.get("environment") not in ("sim", "real"):
        raise ValueError("recovery context requires site, robot, embodiment and sim/real environment")
    return [key + ":" + fp.text(context[key], key, 64) for key in required]


def register(root: str, failure: str, recovery: str, *, context: dict[str, str],
              source_episodes: list[str] | None = None) -> dict[str, Any]:
    fp.text(failure, "failure type", 128)
    fp.text(recovery, "recovery", 1200)
    scopes = [*_scopes(context), "recovery:" + fp.digest({"failure": failure, "recovery": recovery})[:16]]
    fact, _ = hierarchical.add_fact(root, "Recovery for " + failure + ": " + recovery, scopes=scopes)
    body = {"id": fact.id, "failure_type": failure, "recovery": recovery, "context": context,
            "sources": source_episodes or [], "scopes": scopes}
    lineage.attest(root, body, fp.sign(root, body), inputs=source_episodes or [], operation="recovery-memory")
    return fp.append(root, "recovery-registry.jsonl", body)


def trial(root: str, identity: str, occasion: str, *, context: dict[str, str]) -> dict[str, Any]:
    scopes = _scopes(context)
    registry = [r for r in fp.journal(root, "recovery-registry.jsonl") if r["id"] == identity]
    if registry and not any(r["context"] == context for r in registry):
        raise PermissionError("recovery is not visible in this site/robot/embodiment context")
    admitted = search_recipes.admit(root, [identity], context=scopes)
    if identity not in admitted:
        raise PermissionError("recovery is not visible in this site/robot/embodiment context")
    config = holdout_io.load_config(root)
    if identity in safety.protected(root):
        return {"id": identity, "injected": True, "hazard": True, "text": admitted[identity].statement}
    if not config.running:
        raise ValueError("configure a randomized recovery trial before measuring lift")
    withheld = holdout_io.assign_and_log(root, [identity], occasion_id=occasion, rate=config.rate, salt=config.salt,
                                        context="recovery-" + fp.digest(context)[:24])
    return {"id": identity, "injected": identity not in withheld,
            "text": admitted[identity].statement if identity not in withheld else None}


def recall(root: str, failure: dict[str, Any], context: dict[str, str], *, limit: int = 5) -> dict[str, Any]:
    fp.count(limit, 1, 20)
    fp.text(failure["failure_type"], "failure type", 128)
    scopes = _scopes(context)
    registered = {row["id"]: row for row in fp.journal(root, "recovery-registry.jsonl")}
    identities = list(registered)
    fp.count(len(identities), 0, 2000)
    admitted = search_recipes.admit(root, identities, context=scopes)
    measured = evidence.for_lessons(root)
    results = []
    for row in registered.values():
        identity = row["id"]
        if identity not in admitted or row["failure_type"] != failure["failure_type"] or row["context"] != context:
            continue
        effect = measured.get("by_lesson", {}).get(identity, {}) if measured.get("available") else {}
        if effect.get("verdict") == experiment.VERDICT_HURTS and identity not in safety.protected(root):
            continue
        results.append(memory_ifc.label(root, {"id": identity, "text": admitted[identity].statement,
            "effect": effect.get("effect"), "interval": effect.get("ci_95"),
            "n": effect.get("n_injected", 0) + effect.get("n_withheld", 0),
            "hazard": identity in safety.protected(root)}))
    results.sort(key=lambda row: (not row["hazard"], -(row["interval"][0] if row["interval"] else -1), row["id"]))
    protected_rows = [row for row in results if row["hazard"]]
    ordinary = [row for row in results if not row["hazard"]]
    return {"recoveries": [*protected_rows, *ordinary[:max(0, limit - len(protected_rows))]], "context": context,
            "kind": "randomized recovery effect estimates; unmeasured items have null effects"}


def interventions_per_hour(episodes: Sequence[dict[str, Any]]) -> dict[str, Any]:
    report = safety.spi(episodes)
    hours = sum(row["hours"] for row in report["series"])
    interventions = sum(row["interventions"] for row in report["series"])
    return {"interventions": interventions, "hours": hours,
            "interventions_per_hour": interventions / hours if hours else None, "series": report["series"]}
