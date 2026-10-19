"""Reviewed hazard registry and durable safety-decision evidence.

UL 4600-inspired leading indicators are descriptive, not safety certification.
EU Machinery Regulation 2023/1230 Annex III 1.2.1 mappings must be verified
against the Official Journal. Retention floors are 366 days for decisions and
5*366 days for interventions/version tracing. A correction is always appendable.
Hazards are never eligible for randomized removal or unattended lifecycle edits.
"""
from __future__ import annotations

import copy
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from commontrace import frontier_protocol as fp
from commontrace import frontmatter, hierarchical, lesson_admission, lesson_io, memory_authority
from commontrace import transparency_log as log

CLASSES = {"zone", "object", "condition", "human-proximity"}
REGISTRY = "safety-registry.jsonl"


def _content(root: str, identity: str) -> dict[str, Any]:
    path = lesson_io.lesson_path(root, identity)
    if path:
        fm, body = frontmatter.read(path)
        return {"id": identity, "text": body, "revision": lesson_io.current_revision(path)}
    fact = hierarchical.load_facts(root).get(identity)
    if fact:
        return {"id": identity, "text": fact.statement, "revision": fact.revision}
    raise ValueError("hazard memory must already exist in the governed store")


def review(root: str, identity: str, *, reviewer: str, reason: str) -> dict[str, Any]:
    if not memory_authority.is_operator():
        raise PermissionError("hazard review requires operator authority")
    return fp.sign(root, {"kind": "hazard-review", "id": fp.text(identity), "reviewer": fp.text(reviewer),
                          "reason": fp.text(reason), "content_hash": fp.digest(_content(root, identity)),
                          "reviewed_at": fp.now()})


def mark(root: str, identity: str, hazard_class: str, receipt: dict[str, Any], *,
         site: str = "", robot: str = "") -> dict[str, Any]:
    if not memory_authority.is_operator():
        raise PermissionError("only an operator can protect a hazard")
    if hazard_class not in CLASSES:
        raise ValueError("unknown hazard class")
    expected = fp.digest(_content(root, identity))
    body = receipt.get("record", {})
    if (not fp.verify(root, receipt) or body.get("kind") != "hazard-review"
            or body.get("id") != identity or body.get("content_hash") != expected):
        raise PermissionError("hazard marking needs a current content-bound review receipt")
    if len(site) > 128 or len(robot) > 128:
        raise ValueError("site and robot labels exceed 128 characters")
    path = lesson_io.lesson_path(root, identity)
    if path:
        fm, text = frontmatter.read(path)
        if fm.get("status") != "active":
            raise ValueError("hazard lesson must be active")
        if not fm.get("approval_receipt") or not lesson_admission.eligible(root, path, fm, text):
            raise PermissionError("hazard lesson also needs an active lesson-admission receipt")
        fm["core"], fm["memory_class"] = True, "hazard"
        fm.pop("expires", None)
        fm.pop("expires_at", None)
        fm["approval_receipt"] = lesson_admission.issue(root, path, fm, text, actor=body["reviewer"])
        lesson_io.write_lesson(path, fm, text, root=root, actor=body["reviewer"], reason="reviewed hazard protection")
    else:
        fact = hierarchical.load_facts(root)[identity]
        if fact.expires_at or fact.valid_until or fact.status != "active":
            raise ValueError("hazard fact must be active without an automatic expiry or closed validity window")
    result = fp.append(root, REGISTRY, {"kind": "mark", "id": identity,
                       "hazard_class": hazard_class, "review": receipt, "site": site, "robot": robot})
    log.append(root, "hazard-protected", {"id": identity, "review_digest": fp.digest(receipt)})
    return result


_PROTECTED: dict[str, tuple[tuple[int, str], dict[str, dict[str, Any]]]] = {}


def protected(root: str) -> dict[str, dict[str, Any]]:
    """Active hazard registrations, keyed by memory id.

    Every retrieval, assignment and write consults this, so the verified
    registry is cached against the journal's signed checkpoint (its row count
    and chain tip): an unchanged registry costs one stat, any append or edit
    re-verifies every review receipt.
    """
    key = os.path.realpath(root)
    checkpoint = fp.checkpoint(root, REGISTRY)
    cached = _PROTECTED.get(key)
    if cached is not None and cached[0] == checkpoint:
        return copy.deepcopy(cached[1])
    active: dict[str, dict[str, Any]] = {}
    for row in fp.journal(root, REGISTRY):
        if row["kind"] == "mark":
            if not fp.verify(root, row["review"]):
                raise ValueError("invalid hazard review")
            active[row["id"]] = row
        elif row["kind"] == "release":
            active.pop(row["id"], None)
    if len(_PROTECTED) > 64:
        _PROTECTED.clear()
    _PROTECTED[key] = (checkpoint, active)
    return copy.deepcopy(active)


def assert_mutable(root: str, identities: Sequence[str]) -> None:
    if set(identities) & set(protected(root)):
        raise PermissionError("protected hazard cannot be edited, compressed, evicted or consolidated without review")


def release(root: str, identity: str, receipt: dict[str, Any]) -> None:
    if not memory_authority.is_operator() or not fp.verify(root, receipt):
        raise PermissionError("operator review receipt required to release hazard protection")
    body = receipt["record"]
    if body.get("kind") != "hazard-review" or body.get("id") != identity \
            or body.get("content_hash") != fp.digest(_content(root, identity)):
        raise PermissionError("stale or unrelated hazard review")
    fp.append(root, REGISTRY, {"kind": "release", "id": identity, "review": receipt})
    log.append(root, "hazard-review-release", {"id": identity, "review_digest": fp.digest(receipt)})


def context(root: str, site: str = "", robot: str = "") -> dict[str, Any]:
    from commontrace import search_recipes

    for label in (site, robot):
        if label:
            fp.text(label, "site/robot", 128)
    hazards = protected(root)
    identities = [identity for identity, row in hazards.items()
                  if row["site"] in ("", site) and row["robot"] in ("", robot)]
    fp.count(len(identities), 0, 512, "hazard context size")
    # Scope admission occurs before content is read or exposed.
    scopes = [label for label in ("site:" + site if site else "", "robot:" + robot if robot else "") if label]
    facts = search_recipes.admit(root, identities, context=scopes)
    items = [{"id": identity, "text": fact.statement, "hazard": True} for identity, fact in facts.items()]
    for identity in identities:
        path = lesson_io.lesson_path(root, identity)
        if path:
            from commontrace import lesson_cache, memory_control

            fm, body = frontmatter.read(path)
            if lesson_cache.fresh_eligible(path, fm, identity, body=body, root=root) \
                    and memory_control.matches(fm.get("scopes", []), scopes):
                items.append({"id": identity, "text": body, "hazard": True})
    return {"site": site, "robot": robot, "memories": items,
            "notice": "Reviewed hazard context; fresh sensors and the planner remain responsible for motion safety."}


def record(root: str, *, robot: str, decision: str, version: str, intervention: bool = False,
           correction_of: str = "") -> dict[str, Any]:
    if type(intervention) is not bool:
        raise ValueError("intervention must be binary")
    if correction_of and not any(r["id"] == correction_of for r in fp.journal(root, "safety-recorder.jsonl")):
        raise ValueError("correction target is not in the safety recorder")
    identity = "decision-" + fp.digest({"robot": robot, "decision": decision, "at": fp.now()})[:24]
    keep = datetime.now(timezone.utc) + timedelta(days=5 * 366 if intervention else 366)
    body = {"id": identity, "robot": fp.text(robot), "decision": fp.text(decision, maximum=8192),
            "version": fp.text(version), "intervention": intervention, "correction_of": correction_of,
            "retain_until": keep.isoformat(), "trace_retain_until":
            (datetime.now(timezone.utc) + timedelta(days=5 * 366)).isoformat()}
    signed = fp.append(root, "safety-recorder.jsonl", body)
    log.append(root, "safety-decision", {"id": identity, "digest": fp.digest(signed)})
    return signed


def spi(episodes: Sequence[dict[str, Any]]) -> dict[str, Any]:
    fp.count(len(episodes), 0, fp.MAX_ROWS)
    series: dict[str, dict[str, float]] = {}
    for episode in episodes:
        day = fp.moment(episode["timestamp"])[:10]
        hours = fp.number(episode.get("duration_hours", 0), 0, 24)
        near = fp.count(episode.get("near_misses", 0), 0, 1_000_000)
        interventions = fp.count(episode.get("interventions", 0), 0, 1_000_000)
        row = series.setdefault(day, {"hours": 0, "near_misses": 0, "interventions": 0})
        row["hours"] += hours
        row["near_misses"] += near
        row["interventions"] += interventions
    return {"series": [{"date": day, **row, "interventions_per_hour":
                        row["interventions"] / row["hours"] if row["hours"] else None}
                       for day, row in sorted(series.items())], "kind": "descriptive leading indicators"}


def export(root: str) -> dict[str, Any]:
    body = {"hazards": protected(root), "decisions": fp.journal(root, "safety-recorder.jsonl"),
            "tree_head": log.head(root), "exported_at": fp.now()}
    return {"record": body, "signature": fp.sign(root, {"digest": fp.digest(body)})}
