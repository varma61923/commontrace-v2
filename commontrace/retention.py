"""Causal dreaming, randomized rewrite overlays and learned utility retention.

ACE (arXiv:2510.04618) motivates itemized updates, not wholesale rewrites.
Utility predictions are heuristics fitted to randomized AIPW labels; they are
not causal confidence bounds. Rewrite promotion uses the existing experiment
and integrity evidence of record. The original remains immutable while a
reviewed overlay is trialled, so harmful or stale rewrites fall back immediately.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence, cast

from commontrace import aipw, evidence, experiment, frontmatter, holdout_io, integrity, lesson_io, lineage, safety
from commontrace import frontier_protocol as fp


@dataclass(frozen=True)
class UtilityModel:
    weights: tuple[float, ...]
    scale: float
    pooled_effect: float
    n: int

    def predict(self, features: Sequence[float], *, local_n: int = 0) -> float:
        if len(features) != len(self.weights):
            raise ValueError("utility features do not match the fitted model")
        fp.count(local_n, 0, fp.MAX_ROWS)
        linear = math.fsum(weight * fp.number(x, -1, 1, "feature") for weight, x in zip(self.weights, features))
        probability = 1 / (1 + math.exp(-max(-30, min(30, linear))))
        fitted = max(-1, min(1, self.scale * (2 * probability - 1)))
        return (local_n * fitted + 20 * self.pooled_effect) / (local_n + 20)


def fit(observations: Sequence[aipw.Observation], features: Sequence[Sequence[float]], *,
        epochs: int = 20) -> UtilityModel:
    fp.count(len(observations), 2, 10_000)
    fp.count(epochs, 1, 50)
    if len(features) != len(observations):
        raise ValueError("one pre-treatment feature vector required per randomized label")
    dimensions = fp.count(len(features[0]), 1, 16)
    if any(len(row) != dimensions for row in features):
        raise ValueError("inconsistent utility feature dimensions")
    for vector in features:
        for x in vector:
            fp.number(x, -1, 1)
    for row in observations:
        fp.number(row.rate, .01, .99, "randomized holdout rate")
    scores = aipw.aipw_scores(list(observations))
    bound = max(1 / min(row.rate, 1 - row.rate) for row in observations)
    weights = [0.0] * dimensions
    # Bounded soft targets preserve the mean of the randomized pseudo-outcome.
    for epoch in range(epochs):
        step = .1 / math.sqrt(epoch + 1)
        for vector, score in zip(features, scores):
            linear = math.fsum(weight * x for weight, x in zip(weights, vector))
            predicted = 1 / (1 + math.exp(-max(-30, min(30, linear))))
            target = (score + bound) / (2 * bound)
            for j, x in enumerate(vector):
                weights[j] -= step * ((predicted - target) * x + .001 * weights[j])
    return UtilityModel(tuple(weights), bound, max(-1, min(1, math.fsum(scores) / len(scores))), len(scores))


def dream_budget(items: Sequence[dict[str, Any]], *, tokens: int, protected: set[str] | None = None) -> dict[str, Any]:
    fp.count(tokens, 0, 1_000_000)
    fp.count(len(items), 0, 4096)
    protected = protected or set()
    eligible, quarantined = [], []
    for row in items:
        identity = fp.text(row["id"])
        effect = fp.number(row["effect"], -1, 1)
        frequency = fp.number(row["future_retrieval"], 0, 1)
        cost = fp.count(row["tokens"], 1, 1_000_000)
        if identity in protected:
            continue
        if effect < 0:
            quarantined.append(identity)
        else:
            eligible.append((frequency * effect / cost, identity, cost, frequency * effect))
    selected, spent = [], 0
    for _, identity, cost, utility in sorted(eligible, reverse=True):
        if spent + cost <= tokens and utility > 0:
            selected.append({"id": identity, "tokens": cost, "predicted_utility": utility})
            spent += cost
    return {"changes": selected, "quarantine": sorted(quarantined), "tokens": spent,
            "kind": "utility heuristic; changes remain itemized and require randomized rewrite evidence"}


def propose(root: str, slug: str, body: str, *, author: str) -> dict[str, Any]:
    safety.assert_mutable(root, [slug])
    fp.text(body, "rewrite", 64_000)
    path = lesson_io.lesson_path(root, slug)
    if path is None:
        raise ValueError("rewrite original does not exist")
    fm, original = frontmatter.read(path)
    from commontrace import lesson_cache

    if not lesson_cache.fresh_eligible(path, fm, slug, body=original, root=root):
        raise ValueError("rewrite original must pass current governed admission")
    revision = lesson_io.current_revision(path)
    identity = "rewrite-" + fp.digest({"slug": slug, "revision": revision, "body": body})[:24]
    if any(row.get("id") == identity for row in fp.journal(root, "dream-rewrites.jsonl")):
        raise ValueError("rewrite proposal already exists")
    record = {"kind": "proposal", "id": identity, "slug": slug, "original_revision": revision,
              "original": original, "body": body, "author": fp.text(author), "sources": [slug]}
    lineage.attest(root, record, fp.sign(root, record), inputs=[slug], operation="causal-dream-rewrite")
    return fp.append(root, "dream-rewrites.jsonl", record)


def review(root: str, identity: str, *, reviewer: str) -> dict[str, Any]:
    from commontrace import memory_authority

    if memory_authority.WRITER.get()[1] != "operator":
        raise PermissionError("rewrite review requires an operator")
    proposals = [row for row in fp.journal(root, "dream-rewrites.jsonl")
                 if row["kind"] == "proposal" and row["id"] == identity]
    if len(proposals) != 1 or proposals[0]["author"] == reviewer:
        raise ValueError("an independent reviewer must approve an existing rewrite")
    return fp.append(root, "dream-rewrites.jsonl", {"kind": "review", "id": identity, "reviewer": fp.text(reviewer)})


def choose(root: str, identity: str, occasion: str) -> dict[str, Any]:
    events = fp.journal(root, "dream-rewrites.jsonl")
    proposals = [r for r in events if r["kind"] == "proposal" and r["id"] == identity]
    if len(proposals) != 1 or not any(r["kind"] == "review" and r["id"] == identity for r in events):
        raise ValueError("a reviewed rewrite proposal is required")
    proposal = proposals[0]
    safety.assert_mutable(root, [proposal["slug"]])
    path = lesson_io.lesson_path(root, proposal["slug"])
    if path is None or lesson_io.current_revision(path) != proposal["original_revision"]:
        raise ValueError("rewrite original has changed; propose and test a fresh rewrite")
    analysis = evidence.analyse(root)
    report = cast(integrity.IntegrityReport | None, analysis.report)
    effect = next((row for row in analysis.effects if row.lesson_slug == identity), None)
    if effect and effect.verdict == experiment.VERDICT_HURTS:
        return {"id": identity, "body": proposal["original"], "arm": "original", "rollback": True,
                "reason": "rewrite lost its randomized holdout"}
    config = holdout_io.load_config(root)
    if not config.running:
        raise ValueError("a configured randomized experiment is required for rewrite trials")
    withheld = holdout_io.assign_and_log(root, [identity], occasion_id=fp.text(occasion), rate=.5, salt=config.salt,
                                        revisions={identity: fp.digest({"body": proposal["body"],
                                                                       "original": proposal["original"]})})
    original = identity in withheld
    return {"id": identity, "body": proposal["original"] if original else proposal["body"],
            "arm": "original" if original else "rewrite", "rollback": False,
            "promoted": bool(effect and report is not None and report.readable
                             and effect.verdict == experiment.VERDICT_HELPS),
            "note": "ongoing randomized comparison; report the task outcome under the same occasion id"}


def report(root: str, *, tokens: int = 10_000) -> dict[str, Any]:
    from commontrace import causal_gate

    rows = causal_gate.current_rows(root)
    model = causal_gate.fit(rows, causal_gate.sources_for(root, {row.lesson for row in rows}))
    largest = max((p.n_injected + p.n_withheld for p in model.lessons.values()), default=1)
    items = [{"id": identity, "effect": posterior.mean,
              "future_retrieval": (posterior.n_injected + posterior.n_withheld) / largest, "tokens": 256}
             for identity, posterior in model.lessons.items()]
    result = dream_budget(items, tokens=tokens, protected=set(safety.protected(root)))
    result["forecast"] = "historical retrieval share, not a calibrated forecast"
    result["rewrite_trials"] = [r for r in fp.journal(root, "dream-rewrites.jsonl") if r["kind"] == "proposal"]
    return result


def selected(root: str, slug: str) -> dict[str, Any] | None:
    """Production overlay, present only after the original-vs-rewrite trial wins.

    Keep sampling `choose` on an explicit monitoring slice. As soon as the
    evidence no longer supports HELPS, this returns None and preserves original.
    """
    if slug in safety.protected(root):
        return None
    path = lesson_io.lesson_path(root, slug)
    if path is None:
        return None
    events = fp.journal(root, "dream-rewrites.jsonl")
    reviewed = {r["id"] for r in events if r["kind"] == "review"}
    analysis = evidence.analyse(root)
    report = cast(integrity.IntegrityReport | None, analysis.report)
    if report is None or not report.readable:
        return None
    helps = {effect.lesson_slug for effect in analysis.effects if effect.verdict == experiment.VERDICT_HELPS}
    for row in reversed(events):
        if (row["kind"] == "proposal" and row["slug"] == slug and row["id"] in reviewed & helps
                and lesson_io.current_revision(path) == row["original_revision"]):
            return {"id": row["id"], "body": row["body"], "lineage": lineage.labels(root, row["id"])}
    return None


def evict(root: str, model: UtilityModel, items: Sequence[dict[str, Any]], *, token_budget: int) -> dict[str, Any]:
    """Explicit utility-driven fact eviction; holds and hazards always win.

    This is an operator opt-in lifecycle action, never called by normal recall.
    Predictions are heuristics; the original fact history remains append-only.
    """
    from commontrace import hierarchical, incident, memory_authority

    if memory_authority.WRITER.get()[1] != "operator":
        raise PermissionError("utility eviction requires an operator")
    fp.count(token_budget, 0, 1_000_000)
    fp.count(len(items), 0, 4096)
    protected = set(safety.protected(root)) | {i for ids in incident.held(root).values() for i in ids}
    ranked = []
    for item in items:
        identity = fp.text(item["id"])
        cost = fp.count(item["tokens"], 1, 1_000_000)
        frequency = fp.number(item["future_retrieval"], 0, 1)
        uplift = model.predict(item["features"], local_n=item.get("n", 0))
        ranked.append((frequency * max(0, uplift) / cost, identity, cost, uplift))
    used, kept, evicted = 0, [], []
    for _, identity, cost, uplift in sorted(ranked, reverse=True):
        if identity in protected or used + cost <= token_budget and uplift >= 0:
            kept.append(identity)
            used += cost
        else:
            hierarchical.forget_fact(root, identity)
            evicted.append(identity)
    return {"kept": kept, "evicted": evicted, "tokens": used, "kind": "learned utility heuristic",
            "protected_over_budget": used > token_budget}
