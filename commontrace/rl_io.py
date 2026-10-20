"""UniMem governed RL exports from authenticated, complete holdout cohorts.

Raw prompts and responses live in trace/artifact content, never immutable
manifest journals. Every export re-admits its complete cohort before producing
bytes. Deletion invalidates the manifest and removes managed JSONL content;
previously downloaded data needs downstream erasure acknowledgement (G20).
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator, Sequence
from typing import Any

from commontrace import (
    aipw,
    allocation,
    eprocess,
    holdout_io,
    integrity,
    memory_authority,
    memory_control,
    rl_export,
    trace_io,
)
from commontrace import frontier_protocol as fp
from commontrace import nextwave_store as store

TYPE = "unimem.rl-episode"
VERSION = 1


def _canonical(root: str, units: Sequence[str]) -> dict[tuple[str, str], dict[str, Any]]:
    from commontrace.commands.experiment_cmd import _load

    fp.rows(holdout_io.holdout_log_path(root))  # Enforce canonical file size and row bounds before the cached reader.
    fp.rows(holdout_io.outcomes_log_path(root))
    rows, _, corrupt = _load(root)
    if corrupt:
        raise ValueError("UniMem RL export requires a readable canonical holdout log")
    result = {}
    selected = [r for r in rows if r.lesson in units]
    for row in selected:
        identity = row.lesson, row.occasion_id
        if identity in result:
            raise ValueError("UniMem RL export refuses duplicate canonical assignment rows")
        result[identity] = {"unit": row.lesson, "occasion_id": row.occasion_id, "injected": row.injected,
            "rate": row.rate, "salt": row.salt, "outcome": row.succeeded, "context": row.context or "default"}
    report = integrity.audit(selected, schedules=allocation.history(root))
    if selected and not report.readable:
        raise PermissionError("UniMem RL export requires a readable canonical integrity audit")
    return result


def _authorize_unit(root: str, unit: str, context: Sequence[str]) -> str:
    fp.text(unit)
    if unit.startswith("skill:"):
        from commontrace import skill_io
        skill_io._load(root, unit, context)
        return store.reference(skill_io.KIND, unit)
    elif unit.startswith("writer:"):
        writer = unit.partition(":")[2]
        store.get(root, "writers", writer, context=context)
        return store.reference("writers", writer)
    elif unit not in store.admit(root, [unit], context=context):
        raise PermissionError("UniMem RL unit is outside the caller's admitted scope")
    return unit


def capture(root: str, occasion_id: str, unit: str, cluster: str, prompt: str, response: str, *,
            context: Sequence[str] = (), policy_logprobs: Sequence[float] | None = None,
            base_logprobs: Sequence[float] | None = None) -> dict[str, Any]:
    """Bind a response's actual occasion to its authenticated trace receipt.

    Call after reporting the canonical binary outcome. This is instrumentation,
    not proof of agent execution; consumers must deliver the assigned arm.
    """
    fp.text(occasion_id)
    fp.text(cluster)
    fp.text(prompt, "training prompt", 20000)
    fp.text(response, "observed response", 20000)
    source_unit = _authorize_unit(root, unit, context)
    canonical = _canonical(root, [unit]).get((unit, occasion_id))
    if canonical is None or type(canonical["outcome"]) is not bool:
        raise ValueError("UniMem training capture requires a resolved canonical trial occasion")
    rl_export.token_kl(policy_logprobs, base_logprobs, 100)
    trial = {"type": TYPE, "version": VERSION, "occasion_id": occasion_id, "unit": unit, "cluster": cluster,
             "canonical_hash": fp.digest(canonical),
             "policy_logprobs": list(policy_logprobs) if policy_logprobs is not None else None,
             "base_logprobs": list(base_logprobs) if base_logprobs is not None else None}
    from commontrace import lineage

    labels = lineage.labels(root, unit)
    trial["unit_integrity"] = labels["integrity"]
    trial["unit_confidentiality"] = lineage.record(root, unit).get("confidentiality", "unspecified")
    identity = "rl-" + fp.digest([unit, occasion_id])[:32]
    if (memory_authority.lineage_blocked(root, identity)
            or store.latest(root, "rl-captures").get(identity, {}).get("status") == "erased"):
        raise PermissionError("UniMem erased training response identities cannot be reused")
    path = trace_io.write_new(root, title="UniMem randomized response", context=prompt, solution=response,
        trace_id=identity, outcome={"resolved": canonical["outcome"]}, tags=["unimem-rl"],
        extra={"scopes": list(context), "source_traces": [unit],
               "extensions": {"profile": {"procedure": {"rl": trial}}}})
    if path is None:
        existing = store.traces(root).get(identity)
        if (existing is None or existing.get("context_text") != prompt or existing.get("solution_text") != response
                or existing.get("extensions", {}).get("profile", {}).get("procedure", {}).get("rl") != trial
                or existing.get("scopes", []) != list(context)):
            raise ValueError("UniMem response occasion is already bound to different content")
    else:
        store.event(root, "rl-captures", identity, {"filename": os.path.relpath(path, root), "unit": unit,
                    "sources": [source_unit], "scopes": list(context), "status": "active"})
    return {"trace_id": identity, "unit": unit, "occasion_id": occasion_id}


def cohort(root: str, units: Sequence[str], *, context: Sequence[str] = ()) -> list[dict[str, Any]]:
    fp.count(len(units), 1, 64)
    units = sorted(set(units))
    for unit in units:
        _authorize_unit(root, unit, context)
    canonical = _canonical(root, units)
    fp.count(len(canonical), 1, rl_export.MAX_EPISODES)
    result = []
    seen = set()
    traces = store.traces(root)
    visible_ids = [i for i, t in traces.items() if memory_control.matches(t.get("scopes", []), list(context))]
    admitted_ids: set[str] = set()
    for start in range(0, len(visible_ids), 512):
        admitted_ids.update(store.admit(root, visible_ids[start:start+512], context=context))
    from commontrace import lineage

    labels = {unit: lineage.labels(root, unit) for unit in units}
    confidentiality = {unit: lineage.record(root, unit).get("confidentiality", "unspecified") for unit in units}
    for identity, trace in traces.items():
        if not memory_control.matches(trace.get("scopes", []), list(context)):
            continue
        trial = trace.get("extensions", {}).get("profile", {}).get("procedure", {}).get("rl")
        if not isinstance(trial, dict) or trial.get("type") != TYPE or trial.get("version") != VERSION:
            continue
        trial_unit = trial.get("unit")
        if not isinstance(trial_unit, str) or trial_unit not in units:
            continue
        unit = trial_unit
        if identity not in admitted_ids:
            continue
        receipt = trace.get("extensions", {}).get("profile", {}).get("origin", {})
        if not receipt or not memory_authority.verify(root, receipt, memory_authority.trace_record(trace)):
            continue
        key = unit, trial["occasion_id"]
        row = canonical.get(key)
        if row is None or row["outcome"] is None or fp.digest(row) != trial.get("canonical_hash"):
            raise ValueError("UniMem response binding differs from its canonical trial")
        if key in seen or trace.get("outcome", {}).get("resolved") is not row["outcome"]:
            raise ValueError("UniMem response cohort has duplicate or contradictory outcomes")
        if (trial["unit_integrity"] != labels[unit]["integrity"]
                or trial["unit_confidentiality"] != confidentiality[unit]):
            raise PermissionError("UniMem RL source labels changed after response capture")
        seen.add(key)
        episode = {**row, "trace_id": identity, "cluster": trial["cluster"], "prompt": trace["context_text"],
            "response": trace["solution_text"], "scopes": trace.get("scopes", []),
            "integrity": labels[unit]["integrity"], "confidentiality": confidentiality[unit],
            "policy_logprobs": trial.get("policy_logprobs"), "base_logprobs": trial.get("base_logprobs")}
        rl_export.validate(episode)
        result.append(episode)
    if seen != set(canonical):
        raise PermissionError("UniMem RL export requires a complete, live and visible authenticated response cohort")
    return sorted(result, key=lambda row: (row["unit"], row["occasion_id"]))


def _training_quality(root: str, episodes: Sequence[dict[str, Any]]) -> None:
    from commontrace import human_help

    groups: dict[tuple[str, ...], list[str]] = {}
    for episode in episodes:
        groups.setdefault(tuple(sorted(episode["scopes"])), []).extend([episode["unit"], episode["trace_id"]])
    for scopes, identities in groups.items():
        human_help.assert_training(root, sorted(set(identities)), context=scopes)



def _egress(root: str, episodes: Sequence[dict[str, Any]]) -> None:
    from commontrace import memory_ifc, policy_io

    _training_quality(root, episodes)
    canaries = fp.journal(root, "memory-canaries.jsonl")
    fp.count(len(canaries), 0, 4096, "UniMem egress canaries")
    tenants = sorted({c["tenant"] for c in canaries})
    fp.count(len(tenants), 0, 128, "UniMem egress tenant groups")
    for episode in episodes:
        policy_io.assert_export(root, [episode["trace_id"]], context=episode["scopes"])
        for tenant in tenants:
            if memory_ifc.egress_match(root, tenant, episode["prompt"] + "\n" + episode["response"])["alert"]:
                raise PermissionError("UniMem training content failed the information-flow egress check")


def effects(episodes: Sequence[dict[str, Any]], *, alpha: float = .05) -> dict[str, dict[str, Any]]:
    fp.number(alpha, 1e-6, .5)
    units = sorted({e["unit"] for e in episodes})
    result = {}
    for unit in units:
        rows = [e for e in episodes if e["unit"] == unit]
        observations = [aipw.Observation(e["injected"], e["outcome"], e["rate"], e["context"]) for e in rows]
        bound = 1/min(min(e["rate"], 1-e["rate"]) for e in rows)
        effect, low, high = eprocess.interval(aipw.aipw_scores(observations), bound=bound, alpha=alpha/len(units))
        result[unit] = {"effect": effect, "interval": [low, high], "classification": "estimate",
                        "assumptions": "complete randomized cohort; stationary effects; exact propensities"}
    return result


def prepare(root: str, units: Sequence[str], *, format: str = "dpo", context: Sequence[str] = (),
            kl_budget: float = .1, alpha: float = .05, exploitation: float = .8,
            exploration: float = .2, connector: str = "generic") -> dict[str, Any]:
    if format not in rl_export.FORMATS:
        raise ValueError("UniMem RL format must be dpo, advantage or replay")
    if connector not in rl_export.CONNECTORS:
        raise ValueError("UniMem RL connector must be generic, agent-lightning or grader")
    episodes = cohort(root, units, context=context)
    _egress(root, episodes)
    measured = effects(episodes, alpha=alpha)
    kl = {(e["unit"], e["occasion_id"]): rl_export.token_kl(e["policy_logprobs"], e["base_logprobs"], kl_budget)
          for e in episodes}
    if any(k["status"] == "over sampled budget" for k in kl.values()):
        raise PermissionError("UniMem RL export exceeds its sampled token KL budget")
    if format == "dpo":
        rows = rl_export.preferences(episodes, measured, kl_budget=kl_budget)
    else:
        rows = [rl_export.advantage(e) for e in episodes]
        weights = rl_export.priorities(episodes, {u: m["interval"] for u, m in measured.items()},
                                      exploitation=exploitation, exploration=exploration)
        for row, weight, episode in zip(rows, weights, episodes):
            row["meta"]["kl_to_base"] = kl[episode["unit"], row["occasion_id"]]
            row["meta"]["population_effect"] = measured[episode["unit"]]
            row["meta"]["vendor_schema"] = rl_export.VENDOR_SCHEMA_STATUS
            if format == "replay":
                row["replay"] = weight
    # Shards bound both content bytes and source fan-out. No raw training
    # content enters the signed, append-only manifest metadata.
    by_occasion = {(e["unit"], e["occasion_id"]): e["trace_id"] for e in episodes}
    inherited_scopes = sorted({scope for e in episodes for scope in e["scopes"]})
    digest = hashlib.sha256()
    chunks: list[list[dict[str, Any]]] = [[]]
    chunk_size = 0
    for row in rows:
        raw = _line(rl_export.connect(row, connector))
        if chunks[-1] and (chunk_size + len(raw) > 180000 or len(chunks[-1]) >= 64):
            chunks.append([])
            chunk_size = 0
        chunks[-1].append(row)
        chunk_size += len(raw)
        digest.update(raw)
    parts = []
    for chunk in chunks:
        if not chunk:
            continue
        source_ids: set[str] = set()
        for row in chunk:
            meta = row["meta"]
            occasions = meta.get("occasions", [row.get("occasion_id")])
            source_ids.update(by_occasion[meta["unit"], o] for o in occasions)
            source_ids.add(_authorize_unit(root, meta["unit"], context))
        mapped = [rl_export.connect(r, connector) for r in chunk]
        # Bind connector output in the part ID as well as its signed content.
        part = "rl-part-" + fp.digest({"rows": mapped, "connector": connector})[:32]
        store.put(root, "rl-parts", part, {"rows": mapped}, scopes=inherited_scopes,
                  sources=sorted(source_ids), immutable=True)
        parts.append(part)
    fp.count(len(parts), 0, 256)
    # The wire digest covers the actual connector bytes, rather than the
    # generic records used above to build shard/source boundaries.
    digest = hashlib.sha256()
    for row in rows:
        digest.update(_line(rl_export.connect(row, connector)))
    identity = "rl-export-" + fp.digest({"format": format, "content": digest.hexdigest(),
                                        "kl_budget": kl_budget, "connector": connector})[:32]
    value = {"id": identity, "format": format, "parts": parts, "row_count": len(rows), "effects": measured,
             "source_units": sorted(set(units)), "manifest_digest": digest.hexdigest(), "kl_budget": kl_budget,
             "classification": "estimate", "vendor_schema": rl_export.VENDOR_SCHEMA_STATUS,
             "connector": connector,
             "encoding": "SHA256 of canonical UTF-8 JSONL with a newline after each record"}
    indexes = []
    for start in range(0, len(parts), 128):
        references = [store.reference("rl-parts", p) for p in parts[start:start+128]]
        index = "rl-index-" + fp.digest(references)[:32]
        store.put(root, "rl-indexes", index, {"parts": parts[start:start+128]}, scopes=inherited_scopes,
                  sources=references, immutable=True)
        indexes.append(store.reference("rl-indexes", index))
    # Even an empty preference dataset carries governed effect metadata and
    # must be invalidated when its unit is erased.
    indexes.extend(_authorize_unit(root, unit, context) for unit in sorted(set(units)))
    store.put(root, "rl-exports", identity, value, scopes=inherited_scopes,
              sources=indexes, immutable=True)
    return value


def _line(row: dict[str, Any]) -> bytes:
    return (json.dumps(row, sort_keys=True, allow_nan=False, separators=(",", ":")) + "\n").encode()


def lines(root: str, identity: str, *, context: Sequence[str] = ()) -> Iterator[bytes]:
    value = store.get(root, "rl-exports", identity, context=context)
    # Re-admit units as well as source trajectories immediately before egress.
    episodes = cohort(root, value["source_units"], context=context)
    _egress(root, episodes)
    for part in value["parts"]:
        # A fresh manifest read detects deletion between chunks. Already
        # delivered bytes require the downstream propagation channel.
        _training_quality(root, episodes)
        store.get(root, "rl-exports", identity, context=context)
        content = store.get(root, "rl-parts", part, context=context)
        yield b"".join(_line(row) for row in content["rows"])


def status(root: str, identity: str, *, context: Sequence[str] = ()) -> dict[str, Any]:
    state = store.latest(root, "rl-exports").get(identity)
    if state is None or not store.visible(state.get("scopes", []), context):
        raise PermissionError("UniMem export is outside the caller's scope")
    try:
        value = store.get(root, "rl-exports", identity, context=context)
        cohort(root, value["source_units"], context=context)
    except (ValueError, PermissionError):
        return {"id": identity, "status": "invalidated", "retraining_required": True, "classification": "heuristic"}
    return {"id": identity, "status": "active", "manifest_digest": value["manifest_digest"],
            "classification": "estimate"}
