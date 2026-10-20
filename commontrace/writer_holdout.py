"""UniMem nested writer trials and downstream contribution estimates.

Only an explicit reader context changes visibility. Writer masking precedes
memory assignment; excluded memories never acquire a false control observation.
Reliability is a shrinkage estimate for routing, not truth or writer authority.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from commontrace import frontier_protocol as fp
from commontrace import lineage, safety, unit_trials
from commontrace import nextwave_store as store

_ACTIVE: ContextVar[tuple[str, dict[str, bool], str] | None] = ContextVar("unimem_writer_mask", default=None)
MAST = {
    "1.1": "TaskSpecification",
    "1.2": "RoleSpecification",
    "1.3": "StepRepetition",
    "1.4": "HistoryLoss",
    "1.5": "TerminationConditions",
    "2.1": "ConversationReset",
    "2.2": "Clarification",
    "2.3": "Derailment",
    "2.4": "Withholding",
    "2.5": "IgnoredInput",
    "2.6": "ActionReasoningMismatch",
    "3.1": "PrematureTermination",
    "3.2": "WeakVerification",
    "3.3": "NoIncorrectVerification",
}
_RULES = {
    "1.1": ("missing task", "ambiguous task"),
    "1.2": ("unclear role", "role conflict"),
    "1.3": ("repeated step", "looped"),
    "1.4": ("forgot history", "lost history"),
    "1.5": ("no stop condition", "termination condition"),
    "2.1": ("reset conversation",),
    "2.2": ("failed to clarify",),
    "2.3": ("off topic", "derailed"),
    "2.4": ("withheld information",),
    "2.5": ("ignored input",),
    "2.6": ("action disagreed", "reasoning mismatch"),
    "3.1": ("stopped early",),
    "3.2": ("weak verification", "unchecked result"),
    "3.3": ("incorrect verification",),
}


def register(
    root: str,
    writer: str,
    *,
    members: Sequence[str] = (),
    rate: float = 0.5,
    alpha: float = 0.05,
    training_size: int = 100,
    minimum_support: int = 10,
) -> dict[str, Any]:
    """Operator pins a principal or a role's authenticated principal membership."""
    store.operator()
    fp.text(writer, "writer", 96)
    fp.count(len(members), 0, 128)
    principals = sorted(set(fp.text(p) for p in members)) or [writer]
    value: dict[str, Any] = {"writer": writer, "members": principals, "unit": "writer:" + writer}
    store.put(root, "writers", writer, value, immutable=True)
    trial = unit_trials.register(
        root,
        value["unit"],
        fp.digest(value),
        rate=rate,
        alpha=alpha,
        training_size=training_size,
        minimum_support=minimum_support,
    )
    return {**value, "trial": trial}


def writers(root: str) -> list[dict[str, Any]]:
    store.operator()
    return store.list_objects(root, "writers")


@contextmanager
def reader(
    root: str, occasion: str, *, context: str = "default", identities: Sequence[str] = ()
) -> Iterator[dict[str, Any]]:
    """Activate parent assignments before ANY inner memory assignment or read.

    Integrators must use this for the entire team episode and report its outcome
    once. Nested reader contexts are refused to avoid changing delivered arms.
    """
    if _ACTIVE.get() is not None:
        raise ValueError("UniMem writer trial reader contexts cannot nest")
    fp.text(occasion)
    registered = {r["writer"]: r for r in store.list_objects(root, "writers")}
    selected = sorted(set(identities)) if identities else sorted(registered)
    fp.count(len(selected), 1, 128)
    arms: dict[str, bool] = {}
    for identity in selected:
        if identity not in registered:
            raise ValueError("UniMem writer is not preregistered")
        assignment = unit_trials.assign(root, registered[identity]["unit"], occasion, context=context)
        arms[identity] = assignment["injected"]
    state = (os.path.realpath(root), arms, occasion)
    token = _ACTIVE.set(state)
    try:
        yield {"occasion_id": occasion, "writers": arms, "hazards_always_visible": True}
    finally:
        _ACTIVE.reset(token)


def masked_by(root: str, identity: str) -> list[str]:
    state = _ACTIVE.get()
    if state is None or state[0] != os.path.realpath(root) or identity.startswith(unit_trials.PREFIXES):
        return []
    if identity in safety.protected(root):
        return []
    # Include descendants of the writer's output; summaries cannot launder a
    # masked parent's contribution into the visible arm.
    actors = {lineage.record(root, i).get("actor") for i in (identity, *lineage.ancestors(root, identity))}
    registered = {r["writer"]: r for r in store.list_objects(root, "writers")}
    return [w for w, exposed in state[1].items() if not exposed and actors.intersection(registered[w]["members"])]


def read_allowed(root: str, identity: str) -> bool:
    parents = masked_by(root, identity)
    state = _ACTIVE.get()
    if parents and state is not None:
        key = fp.digest([identity, state[2]])
        if key not in store.latest(root, "writer-eligibility"):
            store.event(
                root,
                "writer-eligibility",
                key,
                {
                    "memory": identity,
                    "occasion": state[2],
                    "status": "ineligible (writer-masked)",
                    "parents": ["writer:" + p for p in parents],
                },
            )
    return not parents


def partition(root: str, identities: Sequence[str], occasion: str) -> tuple[list[str], set[str]]:
    """Return eligible inner units and masked IDs, recording only eligibility."""
    if _ACTIVE.get() is None:
        return list(identities), set()
    state = _ACTIVE.get()
    if state is None or state[0] != os.path.realpath(root):
        return list(identities), set()
    if state[2] != occasion:
        raise ValueError("UniMem inner occasion must match its writer parent")
    eligible, masked = [], set()
    for identity in identities:
        parents = masked_by(root, identity)
        if parents:
            masked.add(identity)
            store.event(
                root,
                "writer-eligibility",
                fp.digest([identity, occasion]),
                {
                    "memory": identity,
                    "occasion": occasion,
                    "status": "ineligible (writer-masked)",
                    "parents": ["writer:" + p for p in parents],
                },
            )
        else:
            eligible.append(identity)
    return eligible, masked


def reliability(root: str, writer: str, *, prior_strength: float = 20) -> dict[str, Any]:
    store.operator()
    value = store.get(root, "writers", writer)
    fp.number(prior_strength, 0, 10000)
    report = unit_trials.report(root, value["unit"])
    n = len(unit_trials.rows(root, value["unit"]))
    effect = report.get("effect", 0)
    # promotion.evaluate uses estimate for the AIPW contrast.
    effect = report.get("estimate", effect)
    score = float(effect) * n / max(1, n + prior_strength)
    return {
        "writer": writer,
        "score": score,
        "n": n,
        "effect": report,
        "classification": "estimate",
        "authority": "unchanged",
        "prior_strength": prior_strength,
    }


def rank(
    root: str, identities: Sequence[str], *, weight: float = 0.1, context: Sequence[str] = ()
) -> list[dict[str, Any]]:
    """Explicit admission/routing prior; caller still runs canonical admission."""
    store.operator()
    fp.count(len(identities), 0, 512)
    fp.number(weight, 0, 1)
    values = {v["writer"]: reliability(root, v["writer"])["score"] for v in writers(root)}
    principals = {p: values[w["writer"]] for w in writers(root) for p in w["members"]}
    result = [
        {
            "id": fp.text(i),
            "writer_prior": weight * principals.get(lineage.record(root, i).get("actor"), 0),
            "classification": "estimate",
        }
        for i in store.admit(root, identities, context=context)
    ]
    return sorted(result, key=lambda r: (-r["writer_prior"], r["id"]))


def tag_mast(root: str, incident_id: str, narrative: str, *, tagger: Any = None) -> dict[str, Any]:
    from commontrace import incident

    store.operator()
    incident.snapshot(root, incident_id)  # Require an existing authenticated frozen incident.
    fp.text(narrative, "incident narrative", 20000)
    text = narrative.lower()
    tags = {code for code, phrases in _RULES.items() if any(p in text for p in phrases)}
    if tagger is not None:
        proposed = tagger(narrative)
        if not isinstance(proposed, list) or len(proposed) > 14 or any(t not in MAST for t in proposed):
            raise ValueError("UniMem MAST tagger must return bounded taxonomy codes")
        tags.update(proposed)
    value = {
        "incident": incident_id,
        "tags": [{"code": t, "name": MAST[t]} for t in sorted(tags)],
        "narrative_digest": fp.digest(narrative),
        "classification": "heuristic",
        "taxonomy": "MAST arXiv:2503.13657; 14 modes in three categories",
    }
    store.event(root, "mast-tags", incident_id, value)
    return value


def retrieve(
    root: str,
    query: str,
    *,
    context: Sequence[str] = (),
    weight: float = 0.1,
    minimum_prior: float | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Opt-in prior in canonical admission and retrieval; hazards bypass thresholds."""
    from commontrace import search_recipes

    store.operator()
    fp.count(limit, 1, 100)
    if minimum_prior is not None:
        fp.number(minimum_prior, -1, 1)
    candidates = search_recipes.search(root, query, context=list(context), limit=100)
    priors = {
        r["id"]: r["writer_prior"] for r in rank(root, [r["id"] for r in candidates], weight=weight, context=context)
    }
    protected = safety.protected(root)
    rows = [
        {**row, "writer_prior": priors[row["id"]], "writer_prior_kind": "estimate"}
        for row in candidates
        if row["id"] in priors
        and (minimum_prior is None or priors[row["id"]] >= minimum_prior or row["id"] in protected)
    ]
    return sorted(rows, key=lambda r: (-(r["score"] + r["writer_prior"]), r["id"]))[:limit]


def route(root: str, candidates: Sequence[str]) -> dict[str, Any]:
    """Choose among operator-approved writers using an estimated contribution prior."""
    store.operator()
    fp.count(len(candidates), 1, 128)
    values = [reliability(root, fp.text(w)) for w in candidates]
    chosen = max(values, key=lambda value: (value["score"], value["writer"]))
    return {"writer": chosen["writer"], "score": chosen["score"], "classification": "estimate"}


def mask(
    root: str,
    occasion: str,
    memories: Sequence[str],
    *,
    context: Sequence[str] = (),
    segment: str = "default",
    identities: Sequence[str] = (),
) -> dict[str, Any]:
    """Deliver nested writer/memory arms for a CLI-driven team episode."""
    from commontrace import holdout_io

    store.operator()
    fp.count(len(memories), 1, 256)
    admitted = store.admit(root, memories, context=context)
    if set(admitted) != set(memories):
        raise PermissionError("UniMem writer mask requires an admitted candidate set")
    cfg = holdout_io.load_config(root)
    with reader(root, occasion, context=segment, identities=identities) as parent:
        withheld = holdout_io.assign_and_log(
            root,
            list(admitted),
            occasion_id=occasion,
            rate=cfg.rate,
            salt=cfg.salt,
            context=segment,
            revisions={i: m.get("revision") for i, m in admitted.items()},
        )
        delivered = {i: value for i, value in store.admit(root, memories, context=context).items() if i not in withheld}
        return {
            **parent,
            "deliver": list(delivered.values()),
            "ineligible": [i for i in memories if masked_by(root, i)],
            "instructions": "deliver exactly this arm and report the occasion's canonical outcome",
        }
