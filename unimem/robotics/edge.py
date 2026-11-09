"""Signed offline context updates, witnessed-head continuity and durable outcomes."""

from __future__ import annotations

import json
from typing import Any, Callable, Sequence

from unimem import _jsonl, evidence, safety, search_recipes
from unimem import frontier_protocol as fp
from unimem import transparency_log as log


def publish_update(
    root: str, identities: list[str], *, previous_size: int = 0, context: Sequence[str] = ()
) -> dict[str, Any]:
    from unimem import sentinel_io

    sentinel_io.assert_generalize(root, identities, context=context)
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
            from unimem import memory_authority, policy_io

            if not policy_io.permits(root, memory_authority.fact_record(admitted[identity]), action="export"):
                raise PermissionError("UniMem active finite policy denies this edge export")
        else:
            from unimem import frontmatter, lesson_admission, lesson_io, memory_authority

            path = lesson_io.lesson_path(root, identity)
            if path is None:
                raise PermissionError("edge memory is not governed or visible")
            fm, text = frontmatter.read(path)
            from unimem import policy_io

            if not policy_io.permits(root, {"memory_type": "lesson", "scopes": fm.get("scopes", [])}, action="export"):
                raise PermissionError("UniMem active finite policy denies this edge lesson export")
            if (
                not fm.get("approval_receipt")
                or not lesson_admission.eligible(root, path, fm, text)
                or memory_authority.lineage_blocked(root, identity)
                or not set(fm.get("scopes", [])) <= set(context)
            ):
                raise PermissionError("edge lesson is unapproved, withdrawn or outside the requested scope")
            revision = lesson_io.current_revision(path)
        rows.append({"id": identity, "text": text, "hazard": identity in hazards, "source_revision": revision})
    signed = fp.sign(root, {"type": "edge-context-v1", "items": rows, "issued_at": fp.now()})
    anchor = log.append(root, "edge-context-update", {"manifest_digest": fp.digest(signed)})
    return {
        "manifest": signed,
        "anchor": anchor,
        "consistency": log.consistent(root, previous_size, anchor["head"]["record"]["tree_size"]),
    }


class EdgeCache:
    def __init__(self, root: str, verify_root: str) -> None:
        self.root, self.verify_root = root, verify_root

    def update(self, bundle: dict[str, Any]) -> None:
        try:
            manifest, anchor = bundle["manifest"], bundle["anchor"]
            if (
                not fp.verify(self.verify_root, manifest)
                or not log.verify(self.verify_root, anchor)
                or anchor["statement"]["record"]["statement"]["manifest_digest"] != fp.digest(manifest)
                or manifest["record"]["type"] != "edge-context-v1"
            ):
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
                    if not log.verify_consistency(
                        old["tree_size"], head["tree_size"], old["root_hash"], head["root_hash"], proof["proof"]
                    ):
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


class OutcomeConflict(ValueError):
    """An immutable offline outcome was retried with different meaning."""

    status_code = 409
    code = "learning_conflict"


def _outcome_key(body: dict[str, Any], path: str) -> str:
    if path == "/v1/outcome":
        return fp.digest(["legacy", body["occasion_id"]])
    if path != "/v1/learning/outcome":
        raise ValueError("unsupported queued outcome endpoint")
    context = body.get("context", [])
    if not isinstance(context, list) or len(context) > 128:
        raise ValueError("queued learning scope must be an array of at most 128 labels")
    for label in context:
        fp.text(label, "scope label", 256)
    return fp.digest(["learning", body["experiment_id"], sorted(set(context)), body["occasion_id"]])


class OutcomeQueue:
    def __init__(self, root: str) -> None:
        self.root = root

    def pending(self) -> list[dict[str, Any]]:
        entries, settled, _ = self._state()
        return [row["body"] for key, row in entries.items() if key not in settled]

    def _state(self) -> tuple[dict[str, dict[str, Any]], set[str], list[dict[str, Any]]]:
        entries: dict[str, dict[str, Any]] = {}
        settled: set[str] = set()
        rejected = []
        for row in fp.journal(self.root, "edge-outcomes.jsonl"):
            if row.get("kind") == "enqueue":
                key = _outcome_key(row["body"], row.get("path", "/v1/outcome"))
                if row.get("queue_id", key) != key:
                    raise ValueError("queued outcome identity does not match its body")
                if key in entries and fp.wire(entries[key]["body"]) != fp.wire(row["body"]):
                    raise OutcomeConflict("conflicting queued outcome history")
                entries[key] = {**row, "queue_id": key}
            elif row.get("kind") in {"ack", "reject"}:
                settlement_id = row.get("queue_id")
                if settlement_id is None:
                    # Old settlements apply only to the preceding legacy entry,
                    # never a later experiment that happens to reuse its occasion.
                    preceding = [k for k, entry in entries.items() if entry["occasion_id"] == row["occasion_id"]]
                    if len(preceding) != 1:
                        raise ValueError("legacy outcome settlement is ambiguous")
                    settlement_id = preceding[0]
                if (
                    not isinstance(settlement_id, str)
                    or settlement_id not in entries
                    or entries[settlement_id]["occasion_id"] != row["occasion_id"]
                ):
                    raise ValueError("outcome settlement has no matching queued identity")
                settled.add(settlement_id)
                if row["kind"] == "reject":
                    rejected.append({**row, "body": entries[settlement_id]["body"]})
        return entries, settled, rejected

    def rejected(self) -> list[dict[str, Any]]:
        """Outcomes the gateway definitively refused, with its status and error code, for an operator."""
        _, _, rejected = self._state()
        return [
            {
                "occasion_id": r["occasion_id"],
                "status": r["status"],
                "code": r["code"],
                "body": r["body"],
            }
            for r in rejected
        ]

    def _settle(self, occasion: str, kind: str, queue_id: str | None, **fields: Any) -> None:
        entries, settled, _ = self._state()
        matches = [
            key
            for key, entry in entries.items()
            if key not in settled and entry["occasion_id"] == occasion and (queue_id is None or key == queue_id)
        ]
        if len(matches) > 1:
            raise OutcomeConflict("ambiguous outcome settlement requires queue_id")
        if matches:
            fp.append(
                self.root,
                "edge-outcomes.jsonl",
                {"kind": kind, "occasion_id": occasion, "queue_id": matches[0], **fields},
            )

    def reject(self, occasion: str, status: int, code: str = "", *, queue_id: str | None = None) -> None:
        """Settle a pending outcome the gateway refused, so it never blocks the outcomes queued after it."""
        fp.text(occasion)
        with _jsonl.locked(fp.filename(self.root, "edge-outcomes.jsonl")):
            self._settle(occasion, "reject", queue_id, status=int(status), code=str(code))

    def enqueue(self, body: dict[str, Any], *, path: str = "/v1/outcome") -> str:
        # Freeze caller-owned nested values before waiting for the queue lock.
        if not isinstance(body, dict):
            raise ValueError("queued outcome must be an object")
        encoded = fp.wire(body)
        if len(encoded) > 4096:
            raise ValueError("queued outcome exceeds 4096 bytes")
        body = json.loads(encoded)
        if path not in {"/v1/outcome", "/v1/learning/outcome"}:
            raise ValueError("unsupported queued outcome endpoint")
        if path == "/v1/learning/outcome":
            experiment = body.get("experiment_id")
            if not isinstance(experiment, str):
                raise ValueError("learning outcome requires a pinned experiment_id")
            fp.text(experiment, "pinned experiment_id", 256)
        identity = fp.text(body["occasion_id"])
        kinds = [k for k in ("succeeded", "reward", "metrics") if k in body]
        binary = kinds == ["succeeded"] and type(body["succeeded"]) is bool
        graded = kinds == ["reward"] and type(body["reward"]) in (int, float) and 0 <= body["reward"] <= 1
        measured = kinds == ["metrics"] and isinstance(body["metrics"], dict)
        if not (binary or graded or measured) or len(fp.wire(body)) > 4096:
            raise ValueError("outcome queue requires one bounded outcome: succeeded, reward or metrics")
        key = _outcome_key(body, path)
        with _jsonl.locked(fp.filename(self.root, "edge-outcomes.jsonl")):
            entries, settled, _ = self._state()
            prior = entries.get(key)
            if prior:
                before, after = prior["body"], body
                if path == "/v1/learning/outcome":
                    before = {**before, "context": sorted(set(before.get("context", [])))}
                    after = {**after, "context": sorted(set(after.get("context", [])))}
                if fp.wire(before) != fp.wire(after) or prior.get("path", "/v1/outcome") != path:
                    raise OutcomeConflict("conflicting queued outcome")
                return key
            fp.count(sum(k not in settled for k in entries), 0, 1999, "pending outcomes")
            fp.append(
                self.root,
                "edge-outcomes.jsonl",
                {"kind": "enqueue", "occasion_id": identity, "body": body, "path": path, "queue_id": key},
            )
        return key

    def acknowledge(self, occasion: str, *, queue_id: str | None = None) -> None:
        fp.text(occasion)
        with _jsonl.locked(fp.filename(self.root, "edge-outcomes.jsonl")):
            self._settle(occasion, "ack", queue_id)
            # Acknowledged outcomes live in the gateway; once nothing is pending the
            # queue restarts so a long-lived robot never fills its bounded journal.
            if not self.pending() and fp.checkpoint(self.root, "edge-outcomes.jsonl")[0] >= ROTATE_OUTCOMES_AFTER:
                fp.rotate(self.root, "edge-outcomes.jsonl", [])

    def flush(self, post: Callable[[str, dict[str, Any]], Any]) -> int:
        """Send every pending outcome in order; returns how many the gateway accepted.

        A definitive refusal (`bridge.GatewayRefused`) is settled as rejected and the flush
        continues. A transport failure stops the flush and leaves the rest queued, in order.
        """
        from unimem.robotics.bridge import GatewayRefused

        sent = 0
        # Derive bodies and routes from one snapshot. Concurrent producers may
        # append after this point; they are replayed by the next flush.
        with _jsonl.locked(fp.filename(self.root, "edge-outcomes.jsonl")):
            entries, settled, _ = self._state()
            pending = [(key, row) for key, row in entries.items() if key not in settled]
        for key, entry in pending:
            body = entry["body"]
            path = entry.get("path", "/v1/outcome")
            if path not in {"/v1/outcome", "/v1/learning/outcome"}:
                raise ValueError("unsupported queued outcome endpoint")
            try:
                reply = post(path, body)
                if path == "/v1/learning/outcome":
                    from unimem.client import learning_reply

                    learning_reply(reply)
            except GatewayRefused as exc:
                self.reject(body["occasion_id"], exc.status, exc.code, queue_id=key)
                continue
            self.acknowledge(body["occasion_id"], queue_id=key)
            sent += 1
        return sent
