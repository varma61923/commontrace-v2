"""Pre-action integrity/confidentiality labels and origin-isolated aggregation.

MemoryGraft (arXiv:2512.16962), CaMeL (arXiv:2503.18813), FIDES
(arXiv:2505.23643), RobustRAG (arXiv:2405.15556). Labels are an API contract,
not a sandbox for an arbitrary downstream model. The certified vote margin is
robust to at most f corrupted independent origin groups; it does not certify
semantic truth. Physical motion additionally requires fresh authenticated sensing.
"""
from __future__ import annotations

import base64
import re
import secrets
import unicodedata
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Any

from unimem import frontier_protocol as fp
from unimem import lineage
from unimem import transparency_log as log

QUOTE_LIMIT = 131_072


def label(root: str, item: dict[str, Any], *, identity: str | None = None) -> dict[str, Any]:
    identity = identity or str(item.get("id") or item.get("slug") or "")
    labels = lineage.labels(root, fp.text(identity))
    level = labels["integrity"]
    trusted = lineage.LEVELS.get(level, -1) >= lineage.LEVELS["user"] and not labels["review_required"]
    record = lineage.record(root, identity)
    result = {**item, **labels, "confidentiality": record.get("confidentiality", "unspecified"),
              "procedure": trusted, "presentation": "procedure" if trusted else "quoted-data"}
    if not trusted:
        # Labelling is presentation, never admission: an oversized memory is
        # quoted in bounded form instead of failing the whole retrieval.
        content = str(item.get("text", item.get("body", "")))
        clipped = content[:QUOTE_LIMIT].replace("\x00", "")
        quoted = "\n".join("> " + line for line in clipped.splitlines())
        result["quoted_text"] = quoted + ("\n> [truncated]" if len(content) > QUOTE_LIMIT else "")
    return result


def safe_label(root: str, item: dict[str, Any], *, identity: str | None = None) -> dict[str, Any]:
    """`label` for retrieval paths: an unreadable lineage fails closed, not loudly.

    If the lineage journal cannot be verified the memory is still returned, but
    as untrusted quoted data with the error named, so an integrity fault never
    upgrades trust and never takes retrieval down with it.
    """
    try:
        return label(root, item, identity=identity)
    except (ValueError, OSError, PermissionError, KeyError, TypeError) as exc:
        content = str(item.get("text", item.get("body", "")))[:QUOTE_LIMIT]
        return {**item, "integrity": "unknown", "lineage_digest": "", "review_required": True, "taints": [],
                "confidentiality": "unspecified", "procedure": False, "presentation": "quoted-data",
                "quoted_text": "\n".join("> " + line for line in content.splitlines()),
                "label_error": f"{type(exc).__name__}: {exc}"[:300]}


def aggregate(root: str, identities: Sequence[str], answer: Callable[[Sequence[dict[str, Any]]], str], *,
              high_consequence: bool = False, poisoned_origins: int = 1,
              max_calls: int = 8, context: Sequence[str] = ()) -> dict[str, Any]:
    fp.count(len(identities), 1, 128)
    fp.count(poisoned_origins, 0, 3)
    fp.count(max_calls, 1, 8)
    if not high_consequence:
        return {"skipped": True, "reason": "origin-isolated aggregation is opt-in for high-consequence queries"}
    from unimem import search_recipes

    admitted = search_recipes.admit(root, identities, context=list(context))
    records = lineage.records(root)
    groups: dict[str, list[dict[str, Any]]] = {}
    for identity, fact in admitted.items():
        roots = records.get(identity, {}).get("root_origins", [])
        # Multi-root derivatives cannot bridge isolated origin groups.
        if len(roots) != 1:
            continue
        groups.setdefault(roots[0], []).append(label(root, {"id": identity, "text": fact.statement}))
    if len(groups) > max_calls:
        return {"ask_human": True, "reason": "origin isolation exceeds the configured call budget", "calls": 0}
    answers = [fp.text(answer(groups[source]), "origin answer", 4096) for source in sorted(groups)]
    votes = Counter(answers).most_common()
    top = votes[0][1] if votes else 0
    runner = votes[1][1] if len(votes) > 1 else 0
    certified = len(groups) >= 2 * poisoned_origins + 1 and top - runner > 2 * poisoned_origins
    return {"answer": votes[0][0] if certified else None, "ask_human": not certified, "calls": len(answers),
            "margin": top - runner, "poisoned_origin_bound": poisoned_origins,
            "kind": "vote stability certificate under independent-origin corruption bound; not semantic truth"}


def sensor_corroborated(root: str, memory_id: str, evidence: Sequence[dict[str, Any]], *,
                        max_age_seconds: float = 5) -> bool:
    fp.number(max_age_seconds, .1, 3600)
    fp.count(len(evidence), 0, 128)
    now = datetime.now(timezone.utc)
    for signed in evidence:
        if not fp.verify(root, signed):
            continue
        row = signed["record"]
        if row.get("kind") not in ("in-situ-sensor", "robot-episode", "spatial-observation") \
                or row.get("memory_id") != memory_id:
            continue
        age = (now - datetime.fromisoformat(fp.moment(row["observed_at"]))).total_seconds()
        if 0 <= age <= max_age_seconds and signed.get("authority") in ("tool", "operator", "user"):
            return True
    return False


def create_canary(root: str, tenant: str) -> dict[str, Any]:
    tenant = fp.text(tenant, "tenant", 128)
    token = "CTCANARY" + secrets.token_hex(16)
    fp.append(root, "memory-canaries.jsonl", {"tenant": tenant, "token": token})
    return {"tenant": tenant, "text": token, "procedure": False}


def _canonical(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", value) if unicodedata.category(c) != "Cf").casefold()


def egress_match(root: str, tenant: str, text: str) -> dict[str, Any]:
    fp.text(tenant, "tenant", 128)
    fp.text(text or " ", "egress", 64_000)
    canaries = [row["token"] for row in fp.journal(root, "memory-canaries.jsonl") if row["tenant"] == tenant]
    surfaces, pending = [], [text]
    for _ in range(3):
        following = []
        for value in pending[:32]:
            surfaces.append(_canonical(value))
            for token in re.findall(r"[A-Za-z0-9+/=_-]{16,2048}", value)[:64]:
                for decode in (lambda s: base64.b64decode(s + "=" * (-len(s) % 4), altchars=b"-_", validate=True),
                               bytes.fromhex):
                    try:
                        raw = decode(token)
                        following.append((raw if isinstance(raw, bytes) else bytes(raw)).decode("utf-8"))
                    except (ValueError, UnicodeError):
                        pass
        pending = following[:32]
    matches = [fp.digest(token) for token in canaries if any(_canonical(token) in surface for surface in surfaces)]
    if matches:
        log.append(root, "memory-canary-egress", {"tenant": tenant, "canary_hashes": matches,
                                                 "egress_digest": fp.digest(text)})
    return {"alert": bool(matches), "canary_hashes": matches,
            "kind": "bounded encoded-canary detector; not comprehensive data-loss prevention"}
