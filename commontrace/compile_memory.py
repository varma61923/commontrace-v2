"""Governed open-weight compilation manifests with pluggable trainers.

LoRA, Cartridge-style KV training (arXiv:2506.06266) and AlphaEdit-style atomic
edits are trainer targets, not built-in training algorithms. The tiny CPU trainer
is a deterministic bag-of-words reference artifact used to exercise governance;
it cannot be represented as a trained production LoRA or a model weight edit.
Every source must remain admitted, unchanged, positive and untainted. Serving
requires a separate F2 trial certificate bound to this exact artifact policy.
"""
from __future__ import annotations

from typing import Any, Protocol, Sequence

from commontrace import evidence, frontmatter, holdout_io, lesson_admission, lesson_io, lineage, promotion, safety
from commontrace import frontier_protocol as fp
from commontrace import transparency_log as log


class Trainer(Protocol):
    def train(self, target: str, model: str, sources: Sequence[dict[str, str]]) -> dict[str, Any]: ...


class CPUReferenceTrainer:
    def train(self, target: str, model: str, sources: Sequence[dict[str, str]]) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for row in sources:
            for word in row["text"].casefold().split():
                word = word[:64]
                counts[word] = counts.get(word, 0) + 1
        return {"format": "commontrace.cpu-reference.v1", "target": target, "model": model,
                "token_counts": dict(sorted(counts.items())[:4096]),
                "production_weights": False}


def build(root: str, name: str, sources: Sequence[str], *, target: str, model: str,
          open_weight: bool, trainer: Trainer | None = None) -> dict[str, Any]:
    fp.text(name)
    fp.text(model)
    if open_weight is not True:
        raise PermissionError("parametric compilation supports explicitly declared open-weight models only")
    if target not in ("lora", "kv-prefix", "atomic-edit"):
        raise ValueError("unsupported compilation target")
    ids = sorted(set(fp.text(x) for x in sources))
    fp.count(len(ids), 1, 128)
    from commontrace import sentinel_io

    sentinel_io.assert_generalize(root, ids)
    if any(r["name"] == name for r in fp.journal(root, "compiled-artifacts.jsonl")):
        raise ValueError("compiled artifact names are immutable")
    if set(safety.protected(root)) & set(ids):
        raise PermissionError("hazard memories must remain explicit planner context")
    positive = evidence.graduated(root)
    material = []
    source_hashes = {}
    for identity in ids:
        path = lesson_io.lesson_path(root, identity)
        if path is None or identity not in positive or lineage.labels(root, identity)["review_required"]:
            raise PermissionError("compile only stable, admitted, causally positive lessons")
        fm, body = frontmatter.read(path)
        if not fm.get("approval_receipt") or not lesson_admission.eligible(root, path, fm, body):
            raise PermissionError("source admission is missing or stale")
        revisions = [r for r in fp.rows(lesson_io.revisions_log_path(root))
                     if lesson_io.canonical_slug(r["lesson"]) == identity]
        if len(revisions) > 2:
            raise PermissionError("unstable source requires a fresh stable bundle")
        source_hashes[identity] = fp.digest({"frontmatter": fm, "body": body})
        material.append({"id": identity, "text": body})
    payload = (trainer or CPUReferenceTrainer()).train(target, model, material)
    if len(fp.wire(payload)) > 256_000:
        raise ValueError("trainer manifest exceeds 256 KiB; store weights by immutable external digest")
    artifact = {"id": "compiled-" + fp.digest({"name": name, "sources": source_hashes, "payload": payload})[:32],
                "name": name, "sources": source_hashes, "payload": payload,
                "target": target, "model": model, "open_weight": True,
                "policy_hash": fp.digest({"compiled_payload": payload, "retrieval_sources": source_hashes})}
    lineage.attest(root, artifact, fp.sign(root, artifact), inputs=ids, operation="compile-memory")
    fp.append(root, "compiled-artifacts.jsonl", artifact)
    log.append(root, "compile-artifact", {"id": artifact["id"], "digest": fp.digest(artifact)})
    return artifact


def _artifact(root: str, name: str) -> dict[str, Any]:
    row = next((r for r in fp.journal(root, "compiled-artifacts.jsonl") if r["name"] == name), None)
    if row is None:
        raise ValueError("unknown compiled artifact")
    return row


def stale(root: str, name: str) -> bool:
    row = _artifact(root, name)
    from commontrace import sentinel_io

    sentinel_io.assert_generalize(root, list(row["sources"]))
    if lineage.labels(root, row["id"])["review_required"]:
        return True
    positive = evidence.graduated(root)
    for identity, digest in row["sources"].items():
        path = lesson_io.lesson_path(root, identity)
        if path is None or identity not in positive or lineage.labels(root, identity)["review_required"]:
            return True
        fm, body = frontmatter.read(path)
        if (not lesson_admission.eligible(root, path, fm, body)
                or fp.digest({"frontmatter": fm, "body": body}) != digest):
            return True
    return False


def assign(root: str, name: str, occasion: str) -> dict[str, Any]:
    row = _artifact(root, name)
    if stale(root, name):
        raise PermissionError("compiled source is stale; roll back to retrieval")
    plans = [r for r in fp.journal(root, "compiled-trials.jsonl") if r["artifact"] == row["id"]]
    if not plans:
        raise ValueError("register a compiled-versus-retrieval change plan before occasions")
    assigned = promotion.assign(root, plans[-1]["plan"], occasion, context="default")
    return {"mode": "compiled" if assigned["injected"] else "retrieval", "sources": list(row["sources"]),
            "artifact": row["id"], "occasion_id": occasion}


def serve(root: str, name: str, certificate: dict[str, Any]) -> dict[str, Any]:
    row = _artifact(root, name)
    from commontrace import sentinel_io

    sentinel_io.require_update(root, row["policy_hash"])
    if stale(root, name) or not promotion.verify(root, certificate, artifact_hash=row["policy_hash"]):
        raise PermissionError("serving requires fresh sources and an F2 certificate for this artifact policy")
    return row


def register_trial(root: str, name: str, plan: str, **criteria: Any) -> dict[str, Any]:
    row = _artifact(root, name)
    if stale(root, name):
        raise PermissionError("compiled source is stale")
    if not holdout_io.load_config(root).running:
        raise ValueError("configure a randomized compilation trial")
    result = promotion.register(root, plan, candidate={"default": 1}, incumbent={"default": 0},
        allowed_changes=[row["id"]], monitoring="compiled-versus-retrieval holdout",
        rollback="serve original admitted retrieval sources", artifact_hash=row["policy_hash"], **criteria)
    fp.append(root, "compiled-trials.jsonl", {"artifact": row["id"], "plan": plan})
    return result
