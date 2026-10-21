"""Causal dreaming, randomized rewrite overlays and learned utility retention.

ACE (arXiv:2510.04618) motivates itemized updates, not wholesale rewrites.
Utility predictions are heuristics fitted to randomized AIPW labels; they are
not causal confidence bounds. Rewrite promotion uses the existing experiment
and integrity evidence of record. The original remains immutable while a
reviewed overlay is trialled, so harmful or stale rewrites fall back immediately.
"""
from __future__ import annotations

import copy
import math
import os
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Sequence, cast

from unimem import aipw, evidence, experiment, frontmatter, holdout_io, integrity, lesson_io, lineage, safety
from unimem import frontier_protocol as fp


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


REWRITES = "dream-rewrites.jsonl"


def _blob_path(root: str, digest: str) -> str:
    import os

    from unimem import paths

    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("invalid rewrite content digest")
    path = os.path.join(paths.memory_dir(root), "dream-rewrites", digest + ".txt")
    paths.enforce_boundary(root, path)
    return path


def _put_blob(root: str, text: str) -> str:
    """Content-addressed rewrite text, kept outside the signed journal so erasure can delete it."""
    import hashlib
    import os

    data = text.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    path = _blob_path(root, digest)
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        staging = path + ".partial"
        with open(staging, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, path)
    return digest


def _text(root: str, row: dict[str, Any], field: str) -> str:
    """A proposal's text; legacy rows carried it inline, current rows by digest."""
    import hashlib

    if field in row:
        return str(row[field])
    try:
        with open(_blob_path(root, row[field + "_digest"]), "rb") as stream:
            data = stream.read(256_001)
    except FileNotFoundError:
        raise ValueError("rewrite text was erased") from None
    if len(data) > 256_000 or hashlib.sha256(data).hexdigest() != row[field + "_digest"]:
        raise ValueError("rewrite text does not match its recorded digest")
    return data.decode("utf-8")


def erase_rewrites(root: str, slugs: set[str]) -> list[str]:
    """Delete rewrite texts of erased lessons; journal rows keep only hash tombstones."""
    removed = []
    for row in fp.journal(root, REWRITES):
        if row["kind"] != "proposal" or row["slug"] not in slugs:
            continue
        for field in ("original_digest", "body_digest"):
            if field in row:
                try:
                    os.unlink(_blob_path(root, row[field]))
                    removed.append(row[field])
                except FileNotFoundError:
                    pass
    with fp._GUARD:
        _TRIALS.pop(os.path.realpath(root), None)
    return removed


def propose(root: str, slug: str, body: str, *, author: str) -> dict[str, Any]:
    safety.assert_mutable(root, [slug])
    fp.text(body, "rewrite", 64_000)
    path = lesson_io.lesson_path(root, slug)
    if path is None:
        raise ValueError("rewrite original does not exist")
    fm, original = frontmatter.read(path)
    from unimem import lesson_cache

    if not lesson_cache.fresh_eligible(path, fm, slug, body=original, root=root):
        raise ValueError("rewrite original must pass current governed admission")
    revision = lesson_io.current_revision(path)
    identity = "rewrite-" + fp.digest({"slug": slug, "revision": revision, "body": body})[:24]
    if any(row.get("id") == identity for row in fp.journal(root, REWRITES)):
        raise ValueError("rewrite proposal already exists")
    record = {"kind": "proposal", "id": identity, "slug": slug, "original_revision": revision,
              "original_digest": _put_blob(root, original), "body_digest": _put_blob(root, body),
              "author": fp.text(author), "sources": [slug]}
    lineage.attest(root, record, fp.sign(root, record), inputs=[slug], operation="causal-dream-rewrite")
    return fp.append(root, REWRITES, record)


def review(root: str, identity: str, *, reviewer: str) -> dict[str, Any]:
    from unimem import memory_authority

    if not memory_authority.is_operator():
        raise PermissionError("rewrite review requires an operator")
    proposals = [row for row in fp.journal(root, REWRITES)
                 if row["kind"] == "proposal" and row["id"] == identity]
    if len(proposals) != 1 or proposals[0]["author"] == reviewer:
        raise ValueError("an independent reviewer must approve an existing rewrite")
    return fp.append(root, REWRITES, {"kind": "review", "id": identity, "reviewer": fp.text(reviewer)})


def choose(root: str, identity: str, occasion: str) -> dict[str, Any]:
    events = fp.journal(root, REWRITES)
    proposals = [r for r in events if r["kind"] == "proposal" and r["id"] == identity]
    if len(proposals) != 1 or not any(r["kind"] == "review" and r["id"] == identity for r in events):
        raise ValueError("a reviewed rewrite proposal is required")
    proposal = proposals[0]
    safety.assert_mutable(root, [proposal["slug"]])
    path = lesson_io.lesson_path(root, proposal["slug"])
    if path is None or lesson_io.current_revision(path) != proposal["original_revision"]:
        raise ValueError("rewrite original has changed; propose and test a fresh rewrite")
    original_text, rewrite_text = _text(root, proposal, "original"), _text(root, proposal, "body")
    analysis = evidence.analyse(root)
    report = cast(integrity.IntegrityReport | None, analysis.report)
    effect = next((row for row in analysis.effects if row.lesson_slug == identity), None)
    if effect and effect.verdict == experiment.VERDICT_HURTS:
        return {"id": identity, "body": original_text, "arm": "original", "rollback": True,
                "reason": "rewrite lost its randomized holdout"}
    config = holdout_io.load_config(root)
    if not config.running:
        raise ValueError("a configured randomized experiment is required for rewrite trials")
    withheld = holdout_io.assign_and_log(root, [identity], occasion_id=fp.text(occasion), rate=.5, salt=config.salt,
                                        revisions={identity: fp.digest({"body": rewrite_text,
                                                                       "original": original_text})})
    original = identity in withheld
    return {"id": identity, "body": original_text if original else rewrite_text,
            "arm": "original" if original else "rewrite", "rollback": False,
            "promoted": bool(effect and report is not None and report.readable
                             and effect.verdict == experiment.VERDICT_HELPS),
            "note": "ongoing randomized comparison; report the task outcome under the same occasion id"}


_TRIALS: OrderedDict[str, tuple[tuple[int, str], dict[str, list[dict[str, Any]]]]] = OrderedDict()


def _after_fork() -> None:
    _TRIALS.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def trials(root: str) -> dict[str, list[dict[str, Any]]]:
    """Reviewed rewrite proposals by lesson slug, newest first (cached per journal checkpoint)."""
    key = os.path.realpath(root)
    with fp._GUARD:
        checkpoint = fp.checkpoint(root, REWRITES)
        cached = _TRIALS.get(key)
        if cached is not None and cached[0] == checkpoint:
            _TRIALS.move_to_end(key)
            return copy.deepcopy(cached[1])
        events = fp.journal(root, REWRITES) if checkpoint[0] else []
        reviewed = {r["id"] for r in events if r["kind"] == "review"}
        result: dict[str, list[dict[str, Any]]] = {}
        for row in reversed(events):
            if row["kind"] == "proposal" and row["id"] in reviewed:
                result.setdefault(row["slug"], []).append(row)
        _TRIALS[key] = (checkpoint, result)
        _TRIALS.move_to_end(key)
        while len(_TRIALS) > 64:
            _TRIALS.popitem(last=False)
        return copy.deepcopy(result)


def overlay(root: str, slug: str, occasion: str | None) -> dict[str, Any] | None:
    """The text to deliver for an injected lesson under a reviewed rewrite trial, or None.

    A rewrite that won its randomized comparison (HELPS, readable audit) is
    served; one that lost (HURTS) rolls back to the original; an undecided one
    is randomized per occasion against the original. Without an occasion id or
    a running experiment, the original is served: an untested rewrite is never
    delivered unrandomized. Hazards are never rewritten.
    """
    candidates = trials(root).get(slug)
    if not candidates or slug in safety.protected(root):
        return None
    path = lesson_io.lesson_path(root, slug)
    if path is None:
        return None
    revision = lesson_io.current_revision(path)
    proposal = next((row for row in candidates if row["original_revision"] == revision), None)
    if proposal is None:
        return None
    winner = selected(root, slug)
    if winner is not None:
        return {"id": winner["id"], "body": winner["body"], "arm": "rewrite", "promoted": True}
    if not occasion or not holdout_io.load_config(root).running:
        return None
    choice = choose(root, proposal["id"], occasion)
    if choice["arm"] == "original":
        return None
    return {"id": proposal["id"], "body": choice["body"], "arm": "rewrite", "promoted": False}


def report(root: str, *, tokens: int = 10_000) -> dict[str, Any]:
    from unimem import causal_gate

    rows = causal_gate.current_rows(root)
    model = causal_gate.fit(rows, causal_gate.sources_for(root, {row.lesson for row in rows}))
    largest = max((p.n_injected + p.n_withheld for p in model.lessons.values()), default=1)
    items = [{"id": identity, "effect": posterior.mean,
              "future_retrieval": (posterior.n_injected + posterior.n_withheld) / largest, "tokens": 256}
             for identity, posterior in model.lessons.items()]
    result = dream_budget(items, tokens=tokens, protected=set(safety.protected(root)))
    result["forecast"] = "historical retrieval share, not a calibrated forecast"
    result["rewrite_trials"] = [{key: value for key, value in r.items() if key not in ("original", "body")}
                                for r in fp.journal(root, REWRITES) if r["kind"] == "proposal"]
    return result


def selected(root: str, slug: str) -> dict[str, Any] | None:
    """Production overlay, present only after the original-vs-rewrite trial wins.

    As soon as the evidence no longer supports HELPS, this returns None and the
    original is served again.
    """
    if slug in safety.protected(root):
        return None
    candidates = trials(root).get(slug)
    path = lesson_io.lesson_path(root, slug)
    if not candidates or path is None:
        return None
    analysis = evidence.analyse(root)
    report = cast(integrity.IntegrityReport | None, analysis.report)
    if report is None or not report.readable:
        return None
    helps = {effect.lesson_slug for effect in analysis.effects if effect.verdict == experiment.VERDICT_HELPS}
    revision = lesson_io.current_revision(path)
    for row in candidates:
        if row["id"] in helps and row["original_revision"] == revision:
            try:
                body = _text(root, row, "body")
            except ValueError:
                continue
            return {"id": row["id"], "body": body, "lineage": lineage.labels(root, row["id"])}
    return None


def evict(root: str, model: UtilityModel, items: Sequence[dict[str, Any]], *, token_budget: int) -> dict[str, Any]:
    """Explicit utility-driven fact eviction; holds and hazards always win.

    This is an operator opt-in lifecycle action, never called by normal recall.
    Predictions are heuristics; the original fact history remains append-only.
    """
    from unimem import hierarchical, incident, memory_authority

    if not memory_authority.is_operator():
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


def expire_consent(root: str, receipt: dict[str, Any]) -> dict[str, Any]:
    """Purge expired enrolled sources; report crypto-erasure as conditional evidence."""
    import os
    from pathlib import Path

    from unimem import certified_erasure, incident, memory_authority, nextwave_store, privacy_erasure, purpose

    nextwave_store.operator()
    if purpose.live(receipt):
        raise PermissionError("UniMem live consent cannot be expired by retention")
    identity = receipt["id"]
    prior = nextwave_store.latest(root, "consent-cleanup").get(identity)
    if prior and prior["status"] == "complete":
        return {"receipt_id": identity, "status": "complete", "certificate": prior.get("certificate")}
    if prior and prior.get("local_purge"):
        return {"receipt_id": identity, "status": "pending", "local_purge": True,
                "reason": "subject vault not configured", "certificate": None}
    ids = sorted(receipt["sources"])
    try:
        incident.assert_unpinned(root, ids)
        safety.assert_mutable(root, ids)
        from unimem import person_io
        from unimem.memory_journal import Journal
        from unimem.record_visibility import scope_from_labels

        for key, source in receipt["sources"].items():
            if source["namespace"] == "facts":
                if person_io._fact(root, key) is not None:
                    privacy_erasure.purge_fact(root, key, reason="Purpose consent ended.")
            elif source["namespace"] == "records":
                journal = Journal(root)
                with journal.connect() as db:
                    row = db.execute("SELECT id FROM resource_refs WHERE ref=?", (key,)).fetchone()
                native_id = row[0] if row is not None else source.get("native_id")
                if native_id is not None:
                    journal.forget(native_id, reason="Purpose consent ended.",
                                   scope=scope_from_labels(source["scopes"]))
            elif source["namespace"] == "lessons":
                path = lesson_io.lesson_path(root, key)
                if path:
                    from unimem import lesson_admission

                    lesson_admission.revoke(root, path, actor="consent-expiry")
                    os.unlink(path)
                erase_rewrites(root, {key})
                privacy_erasure._purge_mirrors(root, {key})
            elif source["namespace"] == "controls":
                privacy_erasure._purge_mirrors(root, {key})
            elif source["namespace"] == "traces":
                from unimem import paths, trace_io

                for trace_path in Path(paths.traces_dir(root)).glob("*.md"):
                    if trace_path.name != "README.md" and trace_io.read(str(trace_path))[0].get("id") == key:
                        paths.enforce_boundary(root, str(trace_path))
                        os.unlink(trace_path)
                privacy_erasure._purge_mirrors(root, {key})
            memory_authority.record_forgetting(root, key, forgotten=True)
        nextwave_store.erase(root, ids)
        if not receipt["vault_directory"]:
            # Local plaintext deletion is real; a missing vault cannot establish
            # destruction of subject wrapping keys or historical backup bytes.
            nextwave_store.event(root, "consent-cleanup", identity, {"status": "pending", "local_purge": True})
            return {"receipt_id": identity, "status": "pending", "local_purge": True,
                    "reason": "subject vault not configured", "certificate": None}
        vault = certified_erasure.SubjectVault(root, receipt["vault_directory"])
        certificate = certified_erasure.erase(root, "consent:"+identity, ids, vault, reason="Purpose consent ended.")
        nextwave_store.event(root, "consent-cleanup", identity, {"status": "complete", "certificate": certificate})
        return {"receipt_id": identity, "status": "complete", "certificate": certificate}
    except (PermissionError, ValueError, OSError, RuntimeError) as error:
        return {"receipt_id": identity, "status": "pending", "reason": type(error).__name__, "certificate": None}
