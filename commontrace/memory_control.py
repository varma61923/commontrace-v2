"""Markdown-first directives, standing questions, proposals and foresight.

Each change is a new Markdown revision. JSON/SQLite state is a rebuildable
projection; generated answers retain evidence and are never promoted to rules.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone

from commontrace import _jsonl, frontmatter, hierarchical, lesson_cache, observations, paths
from commontrace.search_recipes import search, terms

KINDS = ("directive", "mental-model", "proposal", "foresight", "watermark")
DIMENSIONS = ("user", "agent", "app", "project", "session")


def scopes(values: dict[str, str]) -> list[str]:
    """Orthogonal routing labels. Access must be checked independently by callers."""
    if set(values) - set(DIMENSIONS):
        raise ValueError("unknown scope dimension")
    if any(not isinstance(v, str) or not re.fullmatch(r"[\w.@:-]{1,128}", v) for v in values.values()):
        raise ValueError("scope values must be 1-128 identifier characters")
    return sorted(f"{k}:{v}" for k, v in values.items())


def matches(labels: list[str], context: list[str]) -> bool:
    """Every declared orthogonal dimension must match; plain legacy scopes are OR."""
    orthogonal = [s for s in labels if s.split(":", 1)[0] in DIMENSIONS and ":" in s]
    legacy = [s for s in labels if s not in orthogonal]
    return set(orthogonal) <= set(context) and (not legacy or bool(set(legacy) & set(context)))


def _directory(root: str, kind: str) -> str:
    if kind not in KINDS:
        raise ValueError("unknown memory control kind")
    return os.path.join(paths.memory_dir(root), "controls", kind)


def put(root: str, kind: str, text: str, *, record_id: str | None = None,
        labels: list[str] | None = None, actor: str = "local", data: dict | None = None) -> dict:
    if not isinstance(text, str) or not text.strip() or len(text) > 20000:
        raise ValueError("text must contain 1-20000 characters")
    rid = record_id or uuid.uuid4().hex
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", rid):
        raise ValueError("invalid record id")
    directory = _directory(root, kind)
    row = {"id": rid, "kind": kind, "recorded_at": datetime.now(timezone.utc).isoformat(),
           "revision": uuid.uuid4().hex, "scopes": sorted(set(labels or [])), "actor": actor,
           "data": data or {}}
    filename = os.path.join(directory, row["revision"] + ".md")
    with _jsonl.locked(directory):
        frontmatter.write(filename, row, text.strip() + "\n")
    return {**row, "text": text.strip()}


def records(root: str, kind: str, *, context: list[str] | None = None) -> list[dict]:
    directory = _directory(root, kind)
    latest: dict[str, dict] = {}
    if not os.path.isdir(directory):
        return []
    for filename in sorted(os.listdir(directory)):
        if not filename.endswith(".md"):
            continue
        fm, text = frontmatter.read(os.path.join(directory, filename))
        if not isinstance(fm.get("id"), str) or not isinstance(fm.get("recorded_at"), str):
            raise ValueError("invalid control revision")
        row = {**fm, "text": text.strip()}
        before = latest.get(row["id"])
        if before is None or (row["recorded_at"], row["revision"]) > (before["recorded_at"], before["revision"]):
            latest[row["id"]] = row
    return [row for row in latest.values() if context is None or matches(row.get("scopes", []), context)]


def directive(root: str, text: str, *, deny_tools: list[str] | None = None,
              required_tags: list[str] | None = None, **options) -> dict:
    if any(values is not None and not isinstance(values, list) for values in (deny_tools, required_tags)):
        raise ValueError("rule identifiers must be lists")
    for values in [*(deny_tools or []), *(required_tags or [])]:
        if not isinstance(values, str) or not re.fullmatch(r"[\w.:-]{1,128}", values):
            raise ValueError("rule identifiers must be nonempty identifiers")
    return put(root, "directive", text, data={"deny_tools": deny_tools or [],
                                             "required_tags": required_tags or []}, **options)


def check_action(root: str, tool: str, *, tags: list[str] | None = None, context: list[str] | None = None) -> None:
    """Machine-enforced rules for integrations that use this action gate."""
    for rule in records(root, "directive", context=context or []):
        data = rule["data"]
        if tool in data.get("deny_tools", []) or not set(data.get("required_tags", [])) <= set(tags or []):
            raise PermissionError(f"action blocked by directive {rule['id']}")


def profile(root: str, query: str, *, context: list[str] | None = None, limit: int = 10,
            occasion_id: str | None = None) -> dict:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 0 <= limit <= 1000:
        raise ValueError("profile limit must be in 0..1000")
    # One canonical read supplies both stable and changing facts.
    now = datetime.now(timezone.utc).isoformat()
    facts = [f for f in hierarchical.list_facts(root, as_of=now)
             if f.status == "active" and matches(f.scopes, context or [])]
    q = set(terms(query))
    facts.sort(key=lambda f: (-len(q & set(terms(f.statement))), -f.confidence, f.id))
    def render(f):
        return {"id": f.id, "text": f.statement, "valid_from": f.valid_from,
                "recorded_at": f.created_at, "expires_at": f.expires_at, "sources": f.source_traces}
    selected = [f for f in facts if f.stability == "stable"][:limit] + [
        f for f in facts if f.stability != "stable"][:limit]
    from commontrace.measure import CausalMemory

    occasion = occasion_id or "profile-" + uuid.uuid4().hex
    recalled = CausalMemory(lambda _query: selected, root=root, key=lambda f: f.id,
                           text=lambda f: f.statement, screen=True, check_every=1).recall_detailed(
                               query, occasion_id=occasion)
    eligible = {f.id for f in recalled.items}
    return {"static": [render(f) for f in selected if f.stability == "stable" and f.id in eligible],
            "dynamic": [render(f) for f in selected if f.stability != "stable" and f.id in eligible],
            "directives": records(root, "directive", context=context or []), "occasion_id": occasion,
            "withheld": [f.id for f in selected if f.id not in eligible and f.id not in recalled.withdrawn]}


def reflect(root: str, query: str, *, context: list[str] | None = None, budget: int = 600,
            occasion_id: str | None = None, causal: bool = True) -> dict:
    """Curated → consolidated → raw, with one shared conservative token budget."""
    if not isinstance(budget, int) or isinstance(budget, bool) or not 0 <= budget <= 100000:
        raise ValueError("budget must be an integer in 0..100000")
    context = context or []
    q = set(terms(query))
    layers: list[tuple[str, list[dict]]] = []
    curated = []
    from commontrace import injection_guard, lesson_admission, ttl

    # Recall may target a signed read-only attachment. Scan source files without
    # materializing the regular retrieval cache inside that repository.
    listing = lesson_cache.listing(root) if os.path.isdir(paths.lessons_dir(root)) else ()
    for filename, _mtime, _size in listing:
        try:
            lesson_admission.validate_path(root, filename)
            fm, body = frontmatter.read(filename)
        except (ValueError, OSError):
            continue
        if not lesson_cache.fresh_eligible(filename, fm, str(fm.get("name", "")), body=body, root=root):
            continue
        if matches(fm.get("scopes", []), context) and q & set(terms(body)):
            curated.append({"id": str(fm.get("name") or os.path.basename(filename)), "text": body,
                            "sources": fm.get("source_traces", [])})
    now = datetime.now(timezone.utc).isoformat()
    current_facts = {f.id: f for f in hierarchical.list_facts(root, as_of=now)
                     if f.status == "active" and matches(f.scopes, context)}
    consolidated = [{"id": o.id, "text": o.statement, "proof_count": o.proof_count,
                     "sources": o.source_fact_ids} for o in observations.load_observations(root).values()
                    if matches(o.scopes, context) and q & set(terms(o.statement)) and o.source_fact_ids
                    and all(fid in current_facts and current_facts[fid].statement == o.statement
                            for fid in o.source_fact_ids)]
    eligible = set(current_facts)
    raw = [r for r in search(root, query, limit=1000, context=context) if r["id"] in eligible]
    from commontrace.commands._traces import load_trace_instances

    for trace in load_trace_instances(root):
        text = str(trace.get("context_text", "")) + "\n" + str(trace.get("solution_text", ""))
        if matches(trace.get("scopes", []), context) and q & set(terms(text)) and not ttl.lesson_is_expired(trace):
            raw.append({"id": str(trace["id"]), "text": text, "sources": [str(trace["id"])]})
    layers.extend((("curated", curated), ("consolidated", consolidated), ("raw", raw)))
    selected, rendered = [], []
    # Directives never silently disappear to satisfy a context cap.
    for rule in records(root, "directive", context=context):
        rendered.append(f"[directive:{rule['id']}] {rule['text']}")
    used = sum((len(t.encode()) + 3) // 4 for t in rendered)
    if used > budget:
        raise ValueError("token budget cannot fit mandatory directives")
    consumed: set[str] = set()
    selected_texts: set[str] = set()
    directive_text = list(rendered)
    for layer, candidates in layers:
        candidates.sort(key=lambda r: (-len(q & set(terms(r["text"]))), r["id"]))
        for row in candidates:
            if injection_guard.injection_labels({"text": row["text"]}):
                continue
            if row["id"] in consumed or row["text"] in selected_texts:
                continue
            text = f"[{layer}:{row['id']}] {row['text']}"
            cost = (len(text.encode()) + 3) // 4 + (1 if rendered else 0)
            if used + cost <= budget:
                rendered.append(text)
                selected.append({**row, "layer": layer})
                used += cost
                consumed.update(row.get("sources", []))
                selected_texts.add(row["text"])
    occasion = occasion_id or "reflect-" + uuid.uuid4().hex
    withheld, withdrawn = [], {}
    if causal:
        from commontrace.measure import CausalMemory

        memory = CausalMemory(lambda _query: selected, root=root, screen=True, check_every=1, scorer="hierarchy-v1")
        recalled = memory.recall_detailed(query, occasion_id=occasion)
        kept = {r["id"] for r in recalled.items}
        withdrawn = recalled.withdrawn
        withheld = [r["id"] for r in selected if r["id"] not in kept and r["id"] not in withdrawn]
        selected = recalled.items
        rendered = directive_text + [f"[{r['layer']}:{r['id']}] {r['text']}" for r in selected]
        used = (len("\n".join(rendered).encode()) + 3) // 4
    return {"context": "\n".join(rendered), "tokens_estimate": used, "budget": budget,
            "evidence": selected, "occasion_id": occasion, "withheld": withheld, "withdrawn": withdrawn}


def refresh_model(root: str, model_id: str) -> dict:
    model = next((r for r in records(root, "mental-model") if r["id"] == model_id), None)
    if model is None:
        raise ValueError("unknown mental model")
    result = reflect(root, model["text"], context=model["scopes"],
                     budget=model["data"].get("budget", 600), causal=False)
    return put(root, "mental-model", model["text"], record_id=model_id, labels=model["scopes"],
               data={**model["data"], "answer": result, "refreshed_at": datetime.now(timezone.utc).isoformat()})


def standing_question(root: str, question: str, *, context: list[str] | None = None, budget: int = 600,
                      refresh_seconds: int = 3600) -> dict:
    from commontrace import jobs

    if not 1 <= refresh_seconds <= 31536000 or not 0 <= budget <= 100000:
        raise ValueError("invalid standing question refresh or budget")
    model = put(root, "mental-model", question, labels=context,
                data={"budget": budget, "refresh_seconds": refresh_seconds})
    jobs.enqueue(root, "mental-model", {"id": model["id"]}, dedupe_key="mental-model:" + model["id"])
    return model


def enqueue_refreshes(root: str) -> int:
    from commontrace import jobs

    now = datetime.now(timezone.utc)
    queued = 0
    for row in records(root, "mental-model"):
        last = row["data"].get("refreshed_at")
        if last is None or (now - lesson_cache.parse_moment(last)).total_seconds() >= row["data"]["refresh_seconds"]:
            jobs.enqueue(root, "mental-model", {"id": row["id"]},
                         dedupe_key="mental-model:" + row["id"] + ":" + row["revision"])
            queued += 1
    return queued


def proposal(root: str, text: str, *, sources: list[str], context: list[str] | None = None) -> dict:
    if not sources:
        raise ValueError("proposals require evidence links")
    return put(root, "proposal", text, labels=context, data={"status": "review", "sources": sources})


def reject_proposal(root: str, proposal_id: str, expected_revision: str, reason: str) -> dict:
    with _jsonl.locked(_directory(root, "proposal")):
        old = next((r for r in records(root, "proposal") if r["id"] == proposal_id), None)
        if not old or old["revision"] != expected_revision or old["data"].get("status") != "review":
            raise ValueError("proposal changed or is no longer in review")
        if not reason.strip():
            raise ValueError("rejection requires a reason")
        return put(root, "proposal", old["text"], record_id=proposal_id, labels=old["scopes"],
                   data={**old["data"], "status": "archived", "reason": reason})


def foresight(root: str, text: str, *, valid_from: str, expires_at: str, sources: list[str],
              context: list[str] | None = None) -> dict:
    start, end = lesson_cache.parse_moment(valid_from), lesson_cache.parse_moment(expires_at)
    if end <= start or not sources:
        raise ValueError("foresight requires evidence and an increasing validity window")
    return put(root, "foresight", text, labels=context,
               data={"valid_from": start.isoformat(), "expires_at": end.isoformat(), "sources": sources,
                     "status": "review"})


def distill_session(root: str, session_id: str, entries: list[dict], complete) -> dict:
    """Seal only after successful extraction and persistence, under a session lock."""
    rid = hashlib.sha256(session_id.encode()).hexdigest()
    with _jsonl.locked(os.path.join(_directory(root, "watermark"), rid)):
        prior = next((r for r in records(root, "watermark") if r["id"] == rid), None)
        watermark = prior["data"]["through"] if prior else -1
        if any(not isinstance(e.get("sequence"), int) or isinstance(e["sequence"], bool)
               or not isinstance(e.get("text"), str) for e in entries):
            raise ValueError("entries need integer sequences and text")
        new = sorted((e for e in entries if e["sequence"] > watermark), key=lambda e: e["sequence"])
        if not new:
            return {"sealed_through": watermark, "proposals": []}
        output, _usage = complete("Distill contrastive success/failure lessons; return JSON {\"lessons\": [\"...\"]}.\n"
                                  + json.dumps(new, ensure_ascii=False))
        payload = json.loads(output)
        lessons = payload.get("lessons")
        if not isinstance(lessons, list) or any(not isinstance(t, str) or not t.strip() for t in lessons):
            raise ValueError("invalid distillation result; watermark not advanced")
        proposals = []
        for text in lessons:
            # Deterministic ids make retries safe if a prior write succeeded.
            pid = hashlib.sha256((rid + str(new[-1]["sequence"]) + text).encode()).hexdigest()
            proposals.append(put(root, "proposal", text, record_id=pid, labels=["session:" + session_id],
                                 data={"status": "review", "sources": [str(e["sequence"]) for e in new]}))
        put(root, "watermark", "Successful session distillation", record_id=rid,
            data={"through": new[-1]["sequence"], "session_id": session_id})
        return {"sealed_through": new[-1]["sequence"], "proposals": proposals}


def offline_pass(root: str, *, max_jobs: int = 20, seconds: float = 30) -> dict:
    from commontrace import jobs

    queued = enqueue_refreshes(root)
    report = jobs.run_pending(root, limit=max_jobs, kinds=["mental-model"], time_budget=seconds)
    return {"models_queued": queued, **report}
