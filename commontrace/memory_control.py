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
from contextvars import ContextVar
from datetime import datetime, timezone

from commontrace import _jsonl, frontmatter, hierarchical, lesson_cache, observations, paths
from commontrace.runtime_cache import RuntimeCache
from commontrace.search_recipes import search, terms

KINDS = ("directive", "mental-model", "proposal", "foresight", "watermark", "compression", "wiki")
_PROFILE_ORDER = RuntimeCache(max_entries=128, max_bytes=4*1024*1024, ttl=30,
                             weigh=lambda key, value: len(repr(key).encode())+sum(len(v)+64 for v in value))
DIMENSIONS = ("user", "agent", "app", "project", "session")
REQUIRED_PRINCIPAL_SCOPE: ContextVar[str] = ContextVar("commontrace_required_principal_scope", default="")


def occasion(value: str | None, *, principal: str = "", generate: bool = True) -> str:
    if value is None and generate:
        value = uuid.uuid4().hex
    if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("occasion_id must be nonempty text without control characters")
    if principal and not value.startswith(principal + ":"):
        value = principal + ":" + value
    if len(value) > 256:
        raise ValueError("occasion_id including principal prefix must fit 256 characters")
    return value


def scopes(values: dict[str, str]) -> list[str]:
    """Orthogonal routing labels. Access must be checked independently by callers."""
    if set(values) - set(DIMENSIONS):
        raise ValueError("unknown scope dimension")
    if any(not isinstance(v, str) or not re.fullmatch(r"[\w.@:-]{1,128}", v) for v in values.values()):
        raise ValueError("scope values must be 1-128 identifier characters")
    return sorted(f"{k}:{v}" for k, v in values.items())


def matches(labels: list[str], context: list[str], *, governed: bool = True) -> bool:
    """Every declared orthogonal dimension must match; plain legacy scopes are OR."""
    if governed and REQUIRED_PRINCIPAL_SCOPE.get() and REQUIRED_PRINCIPAL_SCOPE.get() not in labels:
        return False
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
    if labels is not None and (not isinstance(labels, list) or any(not isinstance(s, str) for s in labels)):
        raise ValueError("scope labels must be strings")
    from commontrace import memory_authority, memory_guard

    clean, _ = memory_guard.sanitize_metadata({"text": text, "data": data or {}},
                                             pii=memory_guard.privacy_redaction_enabled())
    text, data = clean["text"], clean["data"]
    rid = record_id or uuid.uuid4().hex
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", rid):
        raise ValueError("invalid record id")
    directory = _directory(root, kind)
    row = {"id": rid, "kind": kind, "recorded_at": datetime.now(timezone.utc).isoformat(),
           "revision": uuid.uuid4().hex, "scopes": sorted(set(labels or [])), "actor": actor,
           "data": data or {}}
    row["origin"] = memory_authority.bind(root, {**row, "text": text.strip()},
                                         sources=(data or {}).get("sources", []))
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
        from commontrace import memory_authority

        if ((row.get("origin") and not memory_authority.verify(root, row["origin"],
                        {k: v for k, v in row.items() if k != "origin"}))
                or (not row.get("origin") and memory_authority.previously_bound(root, row["id"]))):
            if kind == "directive":
                raise PermissionError("mandatory directive has an invalid origin receipt")
            continue
        before = latest.get(row["id"])
        if before is None or (row["recorded_at"], row["revision"]) > (before["recorded_at"], before["revision"]):
            latest[row["id"]] = row
    return [row for row in latest.values() if context is None
            or matches(row.get("scopes", []), context, governed=kind != "directive")]


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
    from commontrace import compression

    promoted = [r for r in compression.active(root, context=context or []) if r["data"]["level"] == "directive"]
    for rule in [*records(root, "directive", context=context or []), *promoted]:
        data = rule["data"]
        if tool in data.get("deny_tools", []) or not set(data.get("required_tags", [])) <= set(tags or []):
            raise PermissionError(f"action blocked by directive {rule['id']}")


def profile(root: str, query: str, *, context: list[str] | None = None, limit: int = 10,
            occasion_id: str | None = None, action_class: str = "") -> dict:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 0 <= limit <= 1000:
        raise ValueError("profile limit must be in 0..1000")
    # One canonical read supplies both stable and changing facts.
    now = datetime.now(timezone.utc).isoformat()
    from commontrace import memory_authority

    facts = [f for f in hierarchical.list_facts(root, as_of=now)
             if f.status == "active" and matches(f.scopes, context or [])
             and memory_authority.permits(root, f, action_class=action_class)]
    q = set(terms(query))
    # Cache only ranking IDs. Canonical expiry, revocation and scope admission
    # above are repeated on every call; holdout assignments below are never cached.
    key = (os.path.abspath(root), query, limit, tuple(context or []), action_class,
           tuple((f.id, f.revision, f.confidence, f.stability) for f in facts))
    order = _PROFILE_ORDER.get_or_load(key, lambda: tuple(f.id for f in sorted(
        facts, key=lambda f: (-len(q & set(terms(f.statement))), -f.confidence, f.id))))
    priority = {fid: index for index, fid in enumerate(order)}
    facts.sort(key=lambda f: priority[f.id])
    def render(f):
        return {"id": f.id, "text": f.statement, "valid_from": f.valid_from,
                "recorded_at": f.created_at, "expires_at": f.expires_at, "sources": f.source_traces}
    selected = [f for f in facts if f.stability == "stable"][:limit] + [
        f for f in facts if f.stability != "stable"][:limit]
    from commontrace.measure import CausalMemory

    occasion_id = occasion(occasion_id)
    occasion_value = occasion_id
    recalled = CausalMemory(lambda _query: selected, root=root, key=lambda f: f.id,
                           text=lambda f: f.statement, screen=True, check_every=1).recall_detailed(
                               query, occasion_id=occasion_value)
    eligible = {f.id for f in recalled.items}
    from commontrace import profile_activity

    if query:
        profile_activity.record(root, "query", query, context or [])
    return {"static": [render(f) for f in selected if f.stability == "stable" and f.id in eligible],
            "dynamic": [render(f) for f in selected if f.stability != "stable" and f.id in eligible],
            "directives": records(root, "directive", context=context or []), "occasion_id": occasion_value,
            "withheld": [f.id for f in selected if f.id not in eligible and f.id not in recalled.withdrawn],
            "recent_activity": profile_activity.recent(root, context or [], limit=min(limit, 100))}


def reflect(root: str, query: str, *, context: list[str] | None = None, budget: int = 600,
            occasion_id: str | None = None, causal: bool = True, exploration_slots: int = 0,
            action_class: str = "", record_receipt: bool = True, adaptive_budget: bool = False,
            max_budget: int | None = None) -> dict:
    """Curated → consolidated → raw, with one shared conservative token budget.

    ``adaptive_budget`` sizes the budget by the question's shape
    (`conversation.search.budget_for`), then doubles it, up to ``max_budget``
    (default 12000), while the selected evidence covers the question weakly and
    candidates were left out for lack of room. ``budget`` in the result is the
    budget used; ``budget_decision`` says how it was reached.
    """
    if not isinstance(budget, int) or isinstance(budget, bool) or not 0 <= budget <= 100000:
        raise ValueError("budget must be an integer in 0..100000")
    if not isinstance(adaptive_budget, bool):
        raise ValueError("adaptive_budget must be true or false")
    decision: dict | None = None
    if adaptive_budget and budget > 0:
        from commontrace import recall as _recall
        from commontrace.conversation.search import budget_for

        if max_budget is not None and (isinstance(max_budget, bool) or not isinstance(max_budget, int)
                                       or not 0 <= max_budget <= 100000):
            raise ValueError("max_budget must be an integer in 0..100000")
        cap = max(budget, _recall.ADAPTIVE_CAP if max_budget is None else max_budget)
        shaped, shape = budget_for(query, budget, cap)
        decision = {"requested": budget, "shaped": shaped, "effective": shaped, "reason": shape, "cap": cap,
                    "grown": []}
        budget = shaped
    if (not isinstance(exploration_slots, int) or isinstance(exploration_slots, bool)
            or not 0 <= exploration_slots <= 100):
        raise ValueError("exploration slots must be an integer in 0..100")
    if exploration_slots and not causal:
        raise ValueError("exploration requires a measured retrieval")
    occasion_id = occasion(occasion_id)
    context = context or []
    q = set(terms(query))
    layers: list[tuple[str, list[dict]]] = []
    curated = []
    from commontrace import injection_guard, lesson_admission, memory_authority, ttl

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
        if not memory_authority.permits_record(root, fm.get("origin", {}),
                        {"id": str(fm.get("name", "")), "text": body}, action_class=action_class):
            continue
        if matches(fm.get("scopes", []), context) and q & set(terms(body)):
            curated.append({"id": str(fm.get("name") or os.path.basename(filename)), "text": body,
                            "sources": fm.get("source_traces", [])})
    now = datetime.now(timezone.utc).isoformat()
    current_facts = {f.id: f for f in hierarchical.list_facts(root, as_of=now)
                     if f.status == "active" and matches(f.scopes, context)
                     and memory_authority.permits(root, f, action_class=action_class)}
    consolidated = [{"id": o.id, "text": o.statement, "proof_count": o.proof_count,
                     "sources": o.source_fact_ids} for o in observations.load_observations(root).values()
                    if matches(o.scopes, context) and q & set(terms(o.statement)) and o.source_fact_ids
                    and all(fid in current_facts and current_facts[fid].statement == o.statement
                            for fid in o.source_fact_ids)]
    eligible = set(current_facts)
    # Layers are re-sorted by query overlap below, so a model reranker here would only add cost.
    raw = [r for r in search(root, query, limit=1000, context=context, reranker="none") if r["id"] in eligible]
    from commontrace.commands._traces import load_trace_instances

    live_ids = set(current_facts)
    for trace in load_trace_instances(root):
        receipt = trace.get("extensions", {}).get("profile", {}).get("origin", {})
        record = memory_authority.trace_record(trace)
        if not memory_authority.permits_record(root, receipt, record, action_class=action_class):
            continue
        if ttl.trace_is_live(trace, now) and matches(trace.get("scopes", []), context):
            live_ids.add(str(trace["id"]))
        text = str(trace.get("context_text", "")) + "\n" + str(trace.get("solution_text", ""))
        if matches(trace.get("scopes", []), context) and q & set(terms(text)) and ttl.trace_is_live(trace, now):
            raw.append({"id": str(trace["id"]), "text": text, "sources": [str(trace["id"])]})
    from commontrace import experience_skills

    for skill in experience_skills.active(root, context=context, action_class=action_class):
        if not memory_authority.permits_record(root, skill.get("origin", {}),
                    {k: v for k, v in skill.items() if k != "origin"}, action_class=action_class):
            continue
        if q & set(terms(skill["text"] + " " + skill["data"]["applies_when"])):
            text = skill["text"] + "\n" + "\n".join(
                f"{i + 1}. {s.get('tool', 'action')}: {s.get('description', '')}"
                for i, s in enumerate(skill["data"]["steps"]))
            curated.append({"id": skill["id"], "text": text, "sources": skill["data"]["sources"]})
    from commontrace import compression

    promoted = compression.active(root, context=context)
    for artifact in promoted:
        if artifact["data"]["level"] == "directive":
            continue  # Added below as mandatory context, outside recall holdout.
        if (q & set(terms(artifact["text"]+" "+artifact["data"]["applies_when"]))
                and memory_authority.permits_record(root, artifact.get("origin", {}),
                    {k: v for k, v in artifact.items() if k != "origin"}, action_class=action_class)):
            target = curated if artifact["data"]["level"] in ("lesson", "skill") else consolidated
            target.append({"id": artifact["id"], "text": artifact["text"], "sources": artifact["data"]["sources"]})
    # Reuse only fresh model snapshots whose selected evidence is still live.
    # The snapshot is a cache of evidence, never a new fact or a directive.
    live_rows = {r["id"]: r for r in [*curated, *raw]}
    for model in records(root, "mental-model", context=context):
        last = model["data"].get("refreshed_at")
        age = (lesson_cache.parse_moment(now) - lesson_cache.parse_moment(last)).total_seconds() if last else None
        if age is None or age > model["data"]["refresh_seconds"]:
            continue
        evidence = model["data"].get("answer", {}).get("evidence", [])
        if evidence and all(r["id"] in live_rows and live_rows[r["id"]]["text"] == r["text"] for r in evidence):
            text = "\n".join(r["text"] for r in evidence)
            if q & set(terms(model["text"] + text)):
                consolidated.append({"id": model["id"], "text": text, "sources": [r["id"] for r in evidence]})
    for note in records(root, "foresight", context=context):
        data = note["data"]
        if (data.get("status") == "active" and q & set(terms(note["text"]))
                and lesson_cache.parse_moment(data["valid_from"]) <= lesson_cache.parse_moment(now)
                < lesson_cache.parse_moment(data["expires_at"]) and set(data["sources"]) <= live_ids
                and memory_authority.permits_record(root, note.get("origin", {}),
                    {k: v for k, v in note.items() if k != "origin"}, action_class=action_class)):
            consolidated.append({"id": note["id"], "text": "Anticipatory note: " + note["text"],
                                 "sources": data["sources"]})
    layers.extend((("curated", curated), ("consolidated", consolidated), ("raw", raw)))
    selected, rendered = [], []
    # Directives never silently disappear to satisfy a context cap.
    rules = [*records(root, "directive", context=context),
             *[r for r in promoted if r["data"]["level"] == "directive"]]
    for rule in rules:
        rendered.append(f"[directive:{rule['id']}] {rule['text']}")
    used = sum((len(t.encode()) + 3) // 4 for t in rendered)
    if used > budget:
        raise ValueError("token budget cannot fit mandatory directives")
    occasion_value = occasion_id
    exploration_pool = []
    if exploration_slots:
        from commontrace.measure import HarmWatch

        harmful = HarmWatch(root, check_every=1).current()
        per_slot = (budget - used) // exploration_slots
        for fact in current_facts.values():
            line = f"[exploration:{fact.id}] {fact.statement}"
            cost = (len(line.encode()) + 3) // 4 + 1
            if (cost <= per_slot and fact.id not in harmful
                    and not injection_guard.injection_labels({"text": fact.statement})):
                exploration_pool.append((fact, cost))
    reserve = max((cost for _fact, cost in exploration_pool), default=0) * min(
        exploration_slots, len(exploration_pool))
    directive_text = list(rendered)
    directive_used = used
    for _layer, candidates in layers:
        candidates.sort(key=lambda r: (-len(q & set(terms(r["text"]))), r["id"]))

    def fill(limit: int) -> tuple[list[str], list[dict], int, set[str], int]:
        """Select layer rows best-first within `limit`; also count rows left out for room."""
        rendered, selected, used = list(directive_text), [], directive_used
        consumed: set[str] = set()
        selected_texts: set[str] = set()
        left_out = 0
        for layer, candidates in layers:
            for row in candidates:
                if injection_guard.injection_labels({"text": row["text"]}):
                    continue
                if row["id"] in consumed or row["text"] in selected_texts:
                    continue
                text = f"[{layer}:{row['id']}] {row['text']}"
                cost = (len(text.encode()) + 3) // 4 + (1 if rendered else 0)
                if used + cost <= limit - reserve:
                    rendered.append(text)
                    selected.append({**row, "layer": layer})
                    used += cost
                    consumed.update(row.get("sources", []))
                    selected_texts.add(row["text"])
                else:
                    left_out += 1
        return rendered, selected, used, consumed, left_out

    rendered, selected, used, consumed, left_out = fill(budget)
    if decision is not None:
        from commontrace import recall as _recall

        while budget < decision["cap"] and len(decision["grown"]) < _recall.MAX_GROWTH_STEPS:
            why = _recall.weak_coverage(query, [r["text"] for r in selected])
            if not why:
                break
            if not left_out:
                decision["stopped"] = "every candidate already fits; a larger budget adds nothing"
                break
            grown = min(decision["cap"], budget * 2)
            rendered, selected, used, consumed, left_out = fill(grown)
            decision["grown"].append({"from": budget, "to": grown, "why": why})
            budget = grown
        decision["effective"] = budget
    withheld, withdrawn = [], {}
    ranked_ids = {r["id"] for r in selected}
    if causal:
        from commontrace.measure import CausalMemory

        memory = CausalMemory(lambda _query: selected, root=root, screen=True, check_every=1, scorer="hierarchy-v1")
        recalled = memory.recall_detailed(query, occasion_id=occasion_value)
        kept = {r["id"] for r in recalled.items}
        withdrawn = recalled.withdrawn
        withheld = [r["id"] for r in selected if r["id"] not in kept and r["id"] not in withdrawn]
        selected = recalled.items
        rendered = directive_text + [f"[{r['layer']}:{r['id']}] {r['text']}" for r in selected]
        used = (len("\n".join(rendered).encode()) + 3) // 4
    assignments = []
    if exploration_slots:
        from commontrace import causal_policy, holdout_io, policy

        available = {fact.id: fact for fact, _cost in exploration_pool
                     if fact.id not in consumed and fact.id not in ranked_ids}
        pool_digest = hashlib.sha256(json.dumps(sorted(available)).encode()).hexdigest()
        salt = holdout_io.load_config(root).salt
        seed_bytes = hashlib.sha256((salt + "\0" + occasion_value + "\0exploration-v1").encode()).digest()
        seed = int.from_bytes(seed_bytes, "big")
        assignments = causal_policy.explore(sorted(available), [], slots=exploration_slots, seed=seed,
                                             delivery_policy=policy.delivery_probabilities(root))
        for row in assignments:
            fact = available[row["memory_id"]]
            row.update(pool_sha256=pool_digest, query_sha256=hashlib.sha256(query.encode()).hexdigest(),
                       source_sha256=hashlib.sha256(json.dumps(fact.to_dict(), sort_keys=True).encode()).hexdigest())
        # Commit immutable assignments before exposing any explored content.
        policy.record_assignment(root, occasion_value, assignments,
                                 baseline=selected, scopes=context or [])
        causal_policy.record(root, occasion_value, assignments)
        for row in assignments:
            if row["delivered"]:
                fact = available[row["memory_id"]]
                selected.append({"id": fact.id, "text": fact.statement, "sources": fact.source_traces,
                                 "layer": "exploration"})
                rendered.append(f"[exploration:{fact.id}] {fact.statement}")
            else:
                withheld.append(row["memory_id"])
        used = (len("\n".join(rendered).encode()) + 3) // 4
    if causal and query.strip():
        from commontrace import memory_guard

        query_text = memory_guard.sanitize_metadata({"query": query},
                            pii=memory_guard.privacy_redaction_enabled())[0]["query"]
        query_path = os.path.join(paths.memory_dir(root), "queries.jsonl")
        with _jsonl.locked(query_path):
            if not any(r["occasion_id"] == occasion_value for r in _jsonl.read_rows(query_path)):
                _jsonl.append_row(query_path, {"occasion_id": occasion_value, "query": query_text, "scopes": context,
                    "sources": [r["id"] for r in selected], "recorded_at": now})
    rule_evidence = [{"id": r["id"], "text": r["text"], "layer": "directive"} for r in rules]
    if causal and record_receipt:
        from commontrace import assurance

        assurance.record_recall(root, occasion_value, [*selected, *rule_evidence], "\n".join(rendered), context or [])
    out = {"context": "\n".join(rendered), "tokens_estimate": used, "budget": budget,
           "evidence": selected, "occasion_id": occasion_value, "withheld": withheld, "withdrawn": withdrawn,
           "exploration": assignments, "authority_sources": [r["id"] for r in [*selected, *rule_evidence]]}
    if decision is not None:
        out["budget_decision"] = decision
    return out


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


def enqueue_refreshes(root: str, *, context: list[str] | None = None) -> int:
    from commontrace import jobs
    from commontrace.wiki import enqueue_changed

    now = datetime.now(timezone.utc)
    queued = enqueue_changed(root, context=context)
    for row in records(root, "mental-model", context=context):
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
              context: list[str] | None = None, record_id: str | None = None) -> dict:
    start, end = lesson_cache.parse_moment(valid_from), lesson_cache.parse_moment(expires_at)
    if end <= start or not sources:
        raise ValueError("foresight requires evidence and an increasing validity window")
    return put(root, "foresight", text, labels=context, record_id=record_id,
               data={"valid_from": start.isoformat(), "expires_at": end.isoformat(), "sources": sources,
                     "status": "review"})


def review_foresight(root: str, note_id: str, expected_revision: str, *, actor: str, approve: bool) -> dict:
    from commontrace import approval

    with _jsonl.locked(_directory(root, "foresight")):
        old = next((r for r in records(root, "foresight") if r["id"] == note_id), None)
        if not old or old["revision"] != expected_revision or old["data"].get("status") != "review":
            raise ValueError("foresight changed or is no longer in review")
        approval.check(approval.load_policy(root), slug=note_id, approver=actor, authors=(old["actor"],))
        if not isinstance(approve, bool):
            raise ValueError("approval must be boolean")
        return put(root, "foresight", old["text"], record_id=note_id, actor=actor, labels=old["scopes"],
                   data={**old["data"], "status": "active" if approve else "archived"})


def anticipate(root: str, *, limit: int = 10) -> list[dict]:
    """Draft recurring-query briefs offline; source links and review remain mandatory."""
    from datetime import timedelta

    existing = {r["id"] for r in records(root, "foresight")}
    rows = _jsonl.read_rows(os.path.join(paths.memory_dir(root), "queries.jsonl"))[-1000:]
    proposals = []
    for row in reversed(rows):
        if len(proposals) >= limit:
            break
        if not row["sources"]:
            continue
        rid = hashlib.sha256(json.dumps([row["query"], sorted(row["scopes"])], ensure_ascii=False).encode()).hexdigest()
        if rid in existing:
            continue
        now = datetime.now(timezone.utc)
        proposals.append(foresight(root, "Revisit likely next request: " + row["query"],
            valid_from=now.isoformat(), expires_at=(now + timedelta(days=1)).isoformat(),
            sources=row["sources"], context=row["scopes"], record_id=rid))
        existing.add(rid)
    return proposals


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
    notes = anticipate(root, limit=min(10, max_jobs))
    return {"models_queued": queued, "foresight_proposals": len(notes), **report}
