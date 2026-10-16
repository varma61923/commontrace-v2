"""Signed offline context updates, witnessed-head continuity and durable outcomes."""
from __future__ import annotations

from typing import Any, Callable, Sequence

from commontrace import _jsonl, evidence, safety, search_recipes
from commontrace import frontier_protocol as fp
from commontrace import transparency_log as log


def publish_update(root: str, identities: list[str], *, previous_size: int = 0,
                   context: Sequence[str] = ()) -> dict[str, Any]:
    fp.count(len(identities), 0, 256)
    hazards = safety.protected(root)
    positive = evidence.graduated(root)
    fp.count(len(context), 0, 128)
    for label in context:
        fp.text(label, "scope", 256)
    admitted = search_recipes.admit(root, identities, context=list(context))
    rows = []
    for identity in identities:
        if identity not in hazards and identity not in positive:
            raise PermissionError("edge cache accepts only governed hazards or proven positive facts")
        if identity in admitted:
            text, revision = admitted[identity].statement, admitted[identity].revision
        else:
            from commontrace import frontmatter, lesson_admission, lesson_io, memory_authority

            path = lesson_io.lesson_path(root, identity)
            if path is None:
                raise PermissionError("edge memory is not governed or visible")
            fm, text = frontmatter.read(path)
            if (not fm.get("approval_receipt") or not lesson_admission.eligible(root, path, fm, text)
                    or memory_authority.lineage_blocked(root, identity)
                    or not set(fm.get("scopes", [])) <= set(context)):
                raise PermissionError("edge lesson is unapproved, withdrawn or outside the requested scope")
            revision = lesson_io.current_revision(path)
        rows.append({"id": identity, "text": text,
                     "hazard": identity in hazards, "source_revision": revision})
    signed = fp.sign(root, {"type": "edge-context-v1", "items": rows, "issued_at": fp.now()})
    anchor = log.append(root, "edge-context-update", {"manifest_digest": fp.digest(signed)})
    return {"manifest": signed, "anchor": anchor,
            "consistency": log.consistent(root, previous_size, anchor["head"]["record"]["tree_size"])}


class EdgeCache:
    def __init__(self, root: str, verify_root: str) -> None:
        self.root, self.verify_root = root, verify_root

    def update(self, bundle: dict[str, Any]) -> None:
        try:
            manifest, anchor = bundle["manifest"], bundle["anchor"]
            if (not fp.verify(self.verify_root, manifest) or not log.verify(self.verify_root, anchor)
                    or anchor["statement"]["record"]["statement"]["manifest_digest"] != fp.digest(manifest)
                    or manifest["record"]["type"] != "edge-context-v1"):
                raise PermissionError("unsigned or invalid edge update")
            items = manifest["record"]["items"]
            fp.count(len(items), 0, 256)
            for item in items:
                fp.text(item["id"])
                fp.text(item["text"], "edge memory", 64_000)
                if type(item["hazard"]) is not bool:
                    raise ValueError("binary hazard label required")
            with _jsonl.locked(fp.filename(self.root, "edge-cache.jsonl")):
                previous = [row for row in fp.journal(self.root, "edge-cache.jsonl") if "bundle" in row]
                head = anchor["head"]["record"]
                if previous:
                    old = previous[-1]["bundle"]["anchor"]["head"]["record"]
                    old_anchor = previous[-1]["bundle"]["anchor"]
                    if anchor["leaf"] < old_anchor["leaf"]:
                        raise PermissionError("stale edge update predates the witnessed manifest")
                    proof = bundle["consistency"]
                    if not log.verify_consistency(old["tree_size"], head["tree_size"], old["root_hash"],
                                                  head["root_hash"], proof["proof"]):
                        raise PermissionError("forked or stale edge update conflicts with the witnessed head")
                    if anchor["leaf"] == old_anchor["leaf"]:
                        if fp.digest(manifest) != fp.digest(previous[-1]["bundle"]["manifest"]):
                            raise PermissionError("forked edge manifest at the same witnessed leaf")
                        return
                if len(previous) >= COMPACT_AFTER:
                    # Keep the live state, not the history: the latest verified bundle
                    # plus every hazard any verified bundle ever delivered.
                    hazards = _hazards(previous)
                    fp.rotate(self.root, "edge-cache.jsonl", [{"bundle": bundle, "retained_hazards": hazards}])
                else:
                    fp.append(self.root, "edge-cache.jsonl", {"bundle": bundle})
        except (KeyError, TypeError, ValueError):
            raise PermissionError("invalid edge update envelope") from None

    def recall(self) -> list[dict[str, Any]]:
        history = [row for row in fp.journal(self.root, "edge-cache.jsonl") if "bundle" in row]
        if not history:
            return []
        # A partial refresh cannot evict a hazard. Decommissioning requires an
        # explicit reviewed cache reset outside this unattended update path.
        latest = {item["id"]: item for item in history[-1]["bundle"]["manifest"]["record"]["items"]}
        latest.update({item["id"]: item for item in _hazards(history)})
        return sorted(latest.values(), key=lambda row: (not row["hazard"], row["id"]))


COMPACT_AFTER = 32
ROTATE_OUTCOMES_AFTER = 4096


def _hazards(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for row in history:
        for item in row.get("retained_hazards", []):
            found[item["id"]] = item
        for item in row["bundle"]["manifest"]["record"]["items"]:
            if item["hazard"]:
                found[item["id"]] = item
    return [found[identity] for identity in sorted(found)]


class OutcomeQueue:
    def __init__(self, root: str) -> None:
        self.root = root

    def pending(self) -> list[dict[str, Any]]:
        rows = [r for r in fp.journal(self.root, "edge-outcomes.jsonl") if r.get("kind") in ("enqueue", "ack")]
        acknowledged = {r["occasion_id"] for r in rows if r["kind"] == "ack"}
        return [r["body"] for r in rows if r["kind"] == "enqueue" and r["occasion_id"] not in acknowledged]

    def enqueue(self, body: dict[str, Any]) -> None:
        identity = fp.text(body["occasion_id"])
        if type(body.get("succeeded")) is not bool or len(fp.wire(body)) > 4096:
            raise ValueError("outcome queue requires a bounded binary outcome")
        with _jsonl.locked(fp.filename(self.root, "edge-outcomes.jsonl")):
            rows = fp.journal(self.root, "edge-outcomes.jsonl")
            prior = next((r for r in rows if r.get("kind") == "enqueue" and r["occasion_id"] == identity), None)
            if prior:
                if prior["body"] != body:
                    raise ValueError("conflicting queued outcome")
                return
            fp.count(len(self.pending()), 0, 1999, "pending outcomes")
            fp.append(self.root, "edge-outcomes.jsonl", {"kind": "enqueue", "occasion_id": identity, "body": body})

    def acknowledge(self, occasion: str) -> None:
        fp.text(occasion)
        with _jsonl.locked(fp.filename(self.root, "edge-outcomes.jsonl")):
            if any(r["occasion_id"] == occasion for r in self.pending()):
                fp.append(self.root, "edge-outcomes.jsonl", {"kind": "ack", "occasion_id": occasion})
            # Acknowledged outcomes live in the gateway; once nothing is pending the
            # queue restarts so a long-lived robot never fills its bounded journal.
            if not self.pending() and fp.checkpoint(self.root, "edge-outcomes.jsonl")[0] >= ROTATE_OUTCOMES_AFTER:
                fp.rotate(self.root, "edge-outcomes.jsonl", [])

    def flush(self, post: Callable[[str, dict[str, Any]], Any]) -> int:
        sent = 0
        for body in self.pending():
            post("/v1/outcome", body)
            self.acknowledge(body["occasion_id"])
            sent += 1
        return sent
