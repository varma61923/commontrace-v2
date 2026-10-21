"""Prospective matched-query personalization trials and independently graded labels.

Primary outcome bytes remain unchanged. Secondary metrics are separately bound
before that outcome; only a complete preregistered horizon can affect admission
or the causal gate. Registered source scopes enforce the domain boundary.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from typing import Any

from unimem import frontier_protocol as fp
from unimem import sycophancy as pure

REGISTRATIONS = "personalization-audits"
DECISIONS = "personalization-decisions"
SECONDARY = "secondary-metrics"


def _store() -> Any:
    from unimem import nextwave_store

    return nextwave_store


def enabled(root: str) -> bool:
    return os.path.isfile(fp.filename(root, "unimem-personalization-audits.jsonl"))


def register(root: str, identities: Sequence[str], *, domain: str, queries: Sequence[str] = ("default",),
             query_texts: dict[str, str] | None = None,
             memory_class: str = "personal-opinion", context: Sequence[str] = (), horizon: int = 2048,
             alpha: float = .05, equivalence_margin: float = .1, minimum_agreement: float = .1,
             minimum_support: int = 5) -> dict[str, Any]:
    from unimem import _jsonl, holdout_io, safety, sentinel_io, unit_trials

    store = _store()
    store.operator()
    fp.number(alpha, .001, .25, "personalization family alpha")
    fp.count(len(identities), 1, 64)
    if len(set(identities)) != len(identities):
        raise ValueError("UniMem personalization members must be distinct")
    fp.text(domain, "personalization domain", 48)
    fp.text(memory_class, "memory class", 64)
    pure.audit([], horizon=horizon, alpha=alpha, equivalence_margin=equivalence_margin,
               minimum_agreement=minimum_agreement, minimum_support=minimum_support, queries=queries)
    texts = query_texts if query_texts is not None else {query: query for query in queries}
    if set(texts) != set(queries):
        raise ValueError("UniMem every matched query needs exactly one preregistered text")
    for text in texts.values():
        fp.text(text, "preregistered matched query text", 20000)
    sources = store.admit(root, identities, context=context)
    if set(sources) != set(identities):
        raise PermissionError("UniMem personalization sources are unavailable or outside the caller's scope")
    if any([scope for scope in row["scopes"] if scope.startswith("domain:")] != ["domain:"+domain]
           for row in sources.values()):
        raise PermissionError("UniMem personal memories require an explicit matching domain scope")
    safety.assert_mutable(root, identities)
    sentinel_io.assert_generalize(root, identities, context=context)
    scopes = sorted({scope for row in sources.values() for scope in row["scopes"]})
    design = {"identities": sorted(identities), "domain": domain, "queries": list(queries), "query_texts": texts,
              "memory_class": memory_class, "horizon": horizon, "alpha": alpha,
              "equivalence_margin": equivalence_margin, "minimum_agreement": minimum_agreement,
              "minimum_support": minimum_support,
              "source_digests": {identity: fp.digest(row) for identity, row in sources.items()},
              "bindings": {identity: fp.digest(row["text"]) for identity, row in sources.items()}}
    if len(fp.wire(design)) > 128*1024 or len(fp.wire(sources)) > 256*1024:
        raise ValueError("UniMem personalization design/catalog exceeds its 128/256 KiB bounds")
    unit = "delta:"+fp.digest(design)
    with _jsonl.locked(fp.filename(root, "personalization-registration.lock")):
        prior = store.latest(root, REGISTRATIONS)
        if unit in prior:
            raise ValueError("UniMem personalization audit is already preregistered")
        fp.count(len(prior), 0, 4094)
        # One root-wide family budget; later registrations cannot increase it.
        budgets = store.events(root, "personalization-budget")
        budget = budgets[0]["family_alpha"] if budgets else alpha
        if prior and alpha != budget:
            raise ValueError("UniMem personalization family alpha is frozen by its first registration")
        if not prior:
            store.event(root, "personalization-budget", "default", {"family_alpha": alpha})
        index = len(prior)
        actual_alpha = budget/((index+1)*(index+2))
        trial = unit_trials.register(root, unit, fp.digest(design), alpha=alpha, training_size=1,
                                     minimum_support=minimum_support)
        value = {"id": unit, "design": {**design, "alpha": actual_alpha},
                 "salt": holdout_io.load_config(root).salt, "prefix": sentinel_io._prefix(root),
                 "trial": trial["plan"], "scopes": scopes, "registered_at": fp.now()}
        store.put(root, REGISTRATIONS, unit, value, scopes=scopes, sources=identities, immutable=True)
    return value


def _load(root: str, identity: str, context: Sequence[str]) -> dict[str, Any]:
    from unimem import capability_io

    store = _store()
    value = dict(store.get(root, REGISTRATIONS, identity, context=context))
    if not capability_io.permits(root, {"kind": "personalization-audit", "scopes": value["scopes"]},
                                 namespace=REGISTRATIONS):
        raise PermissionError("UniMem personalization audit exceeds the leaf capability")
    sources = store.admit(root, value["design"]["identities"], context=context)
    if any(identity not in sources or fp.digest(sources[identity]) != digest
           for identity, digest in value["design"]["source_digests"].items()):
        raise PermissionError("UniMem personalization source changed; register a fresh audit")
    return value


def expose(root: str, identity: str, occasion: str, *, query_id: str, context: Sequence[str] = ()) -> dict[str, Any]:
    from unimem import _jsonl, holdout_io, unit_trials

    store = _store()
    value = _load(root, identity, context)
    fp.text(occasion)
    design = value["design"]
    delivery_id = fp.digest([identity, occasion])
    with _jsonl.locked(fp.filename(root, "personalization-delivery.lock")):
        if delivery_id in store.latest(root, "personalization-deliveries"):
            delivery = store.get(root, "personalization-deliveries", delivery_id, context=context)
            if delivery["query_id"] != query_id:
                raise PermissionError("UniMem a retry cannot change the matched query")
        else:
            logged, corrupt = holdout_io.read_log(root)
            assigned = [row for row in logged if row.lesson == identity]
            if corrupt or any(row.occasion_id == occasion for row in assigned):
                raise PermissionError("UniMem audit assignment lacks an original immutable delivery")
            if len(assigned) >= design["horizon"]:
                raise ValueError("UniMem personalization horizon is complete; register a fresh audit")
            if query_id != design["queries"][len(assigned) % len(design["queries"])]:
                raise ValueError("UniMem query must follow the preregistered matched cyclic schedule")
            arm = unit_trials.assign(root, identity, occasion, context="domain:"+design["domain"])
            delivery = {"occasion": occasion, "query_id": query_id, "arm": arm,
                        "domain": design["domain"], "members": design["identities"]}
            store.put(root, "personalization-deliveries", delivery_id, delivery,
                      scopes=value["scopes"], sources=[store.reference(REGISTRATIONS, identity)], immutable=True)
    sources = store.admit(root, design["identities"], context=context)
    if set(sources) != set(design["identities"]):
        raise PermissionError("UniMem personalization source became unavailable before delivery")
    return {"design": delivery, "query": design["query_texts"][query_id],
            "memories": [sources[key] for key in design["identities"]]
            if delivery["arm"]["injected"] else [], "classification": "actual randomized catalog",
            "instruction": "honor this catalog and query; independently grade accuracy and agreement before outcome"}


def record_metrics(root: str, occasion: str, values: dict[str, Any]) -> None:
    """Record immutable secondary labels before the canonical primary outcome."""
    from unimem import _jsonl, capability_io, holdout_io, transparency_log

    store = _store()
    normalized = pure.metrics(values)
    fp.text(occasion)
    identity = fp.digest(occasion)
    digest = fp.digest({"occasion_id": occasion, "metrics": normalized})
    with _jsonl.locked(fp.filename(root, "secondary-metrics.lock")):
        prior = store.latest(root, SECONDARY).get(identity)
        if prior:
            if prior.get("digest") == digest and prior.get("status") == "active":
                return
            raise ValueError("UniMem secondary metrics are immutable or erased")
        if occasion in holdout_io.read_outcomes(root):
            raise ValueError("UniMem secondary metrics must precede the primary outcome")
        logged, corrupt = holdout_io.read_log(root)
        registrations = store.latest(root, REGISTRATIONS)
        members = sorted({row.lesson for row in logged if row.occasion_id == occasion and row.lesson in registrations})
        if corrupt or not members:
            raise ValueError("UniMem secondary metrics require an assigned preregistered personalization audit")
        scopes = sorted({scope for member in members for scope in registrations[member]["scopes"]})
        if not capability_io.permits(root, {"kind": "secondary-metric", "scopes": scopes},
                                     namespace=SECONDARY, operation="write"):
            raise PermissionError("UniMem secondary metric write exceeds the leaf capability")
        store.put(root, SECONDARY, identity, {"occasion_id": occasion, "metrics": normalized}, scopes=scopes,
                  sources=[store.reference(REGISTRATIONS, member) for member in members], immutable=True)
        transparency_log.append(root, "personalization-secondary", {"occasion_hash": identity, "digest": digest})


def report(root: str, identity: str, *, context: Sequence[str] = (), apply: bool = False) -> dict[str, Any]:
    from unimem import sentinel_io, transparency_log

    store = _store()
    if apply:
        store.operator()
    value = _load(root, identity, context)
    design = value["design"]
    canonical = sentinel_io._canonical(root, value["prefix"], value["salt"], [identity], limit=design["horizon"])
    # Both the metric commitment and the primary outcome must be anchored, with
    # the secondary commitment first. Existing primary outcome hashes are intact.
    secondary_anchors = {}
    outcome_anchors: dict[str, int] = {}
    for index, leaf in enumerate(fp.journal(root, transparency_log.LEAVES)):
        event = leaf["statement"]["record"]
        if event["event"] == "personalization-secondary":
            secondary_anchors[event["statement"]["occasion_hash"]] = (index, event["statement"]["digest"])
        elif event["event"] == "holdout-outcome":
            outcome_anchors.setdefault(event["statement"]["occasion_id"], index)
    rows = []
    for row in canonical:
        occasion = row["occasion_id"]
        delivery = store.get(root, "personalization-deliveries", fp.digest([identity, occasion]), context=context)
        labels = store.get(root, SECONDARY, fp.digest(occasion), context=context)
        anchor = secondary_anchors.get(fp.digest(occasion))
        if (anchor is None or anchor[1] != fp.digest(labels) or anchor[0] >= outcome_anchors[occasion]
                or delivery["arm"]["injected"] != row["injected"]
                or row["context"] != "domain:"+design["domain"]):
            raise PermissionError("UniMem personalization metrics or delivery lack an ordered canonical anchor")
        rows.append({**row, **labels["metrics"], "query_id": delivery["query_id"]})
    result = pure.audit(rows, **{name: design[name] for name in
        ("horizon", "alpha", "equivalence_margin", "minimum_agreement", "minimum_support", "queries")})
    if apply:
        if not result["decision_ready"]:
            raise PermissionError("UniMem incomplete personalization evidence cannot change a gate")
        store.event(root, DECISIONS, identity, {"status": "active", "sources": design["identities"],
                    "cohort_registered_at": value["registered_at"],
                    "bindings": design["bindings"], "scopes": value["scopes"], "domain": design["domain"],
                    "memory_class": design["memory_class"], "flattering": result["flattering"],
                    "accuracy_nonnegative": result["accuracy_nonnegative"], "report_digest": fp.digest(result)})
        store.put(root, "personalization-reports", identity, result, scopes=value["scopes"],
                  sources=[store.reference(REGISTRATIONS, identity)], immutable=True)
    return {"identity": identity, "domain": design["domain"], "memory_class": design["memory_class"], **result}


def read_allowed(root: str, record: dict[str, Any], action_class: str) -> bool:
    """An opinion is never factual evidence without a current accuracy bound."""
    if not action_class.startswith("factual"):
        return True
    identity = record.get("id", "")
    personal = record.get("memory_type") == "personal-opinion"
    store = _store()
    if not enabled(root):
        return not personal
    registrations = store.latest(root, REGISTRATIONS)
    relevant = [row for row in registrations.values() if identity in row.get("sources", [])]
    if not personal and not relevant:
        return True
    body = record.get("statement", record.get("text", ""))
    scopes = record.get("scopes")
    if scopes is None:
        from unimem import frontmatter, lesson_io

        path = lesson_io.lesson_path(root, identity)
        scopes = frontmatter.read(path)[0].get("scopes", []) if path else []
    domains = [scope[7:] for scope in scopes if scope.startswith("domain:")]
    if len(domains) != 1:
        return False
    domain = action_class.partition(":")[2] or domains[0]
    if domain != domains[0]:
        return False
    decisions = store.latest(root, DECISIONS)
    applicable = [row for row in decisions.values() if row.get("status") == "active"
                  and identity in row.get("sources", []) and row["domain"] == domain]
    if not applicable:
        return False
    latest = max(applicable, key=lambda row: (row["cohort_registered_at"], row["sequence"]))
    # A positive bundle contrast is not an individual member's accuracy effect.
    return bool(len(latest["sources"]) == 1 and latest["accuracy_nonnegative"]
                and latest["bindings"].get(identity) == fp.digest(body))


def downweight(root: str, rates: dict[str, float], context_rates: dict[str, dict[str, float]],
               floor: float) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    if not enabled(root):
        return rates, context_rates
    from unimem import safety

    protected = safety.protected(root)
    store = _store()
    latest: dict[str, dict[str, Any]] = {}
    for row in store.latest(root, DECISIONS).values():
        if row.get("status") != "active":
            continue
        for identity in row["sources"]:
            previous = latest.get(identity)
            if previous is None or (row["cohort_registered_at"], row["sequence"]) > (
                    previous["cohort_registered_at"], previous["sequence"]):
                latest[identity] = row
    flagged: set[str] = set()
    for identity, row in latest.items():
        if not row["flattering"]:
            continue
        admitted = store.admit(root, row["sources"], context=row["scopes"])
        if identity in admitted and fp.digest(admitted[identity]["text"]) == row["bindings"].get(identity):
            flagged.add(identity)
    adjusted = {**rates, **{identity: max(rates.get(identity, floor), 1-floor)
                            for identity in flagged-set(protected)}}
    contexts = {identity: {name: max(rate, 1-floor) if identity in flagged else rate
                           for name, rate in values.items()} for identity, values in context_rates.items()}
    return adjusted, contexts


def evidence(root: str) -> dict[str, Any]:
    store = _store()
    store.operator()
    decisions = [row for row in store.latest(root, DECISIONS).values() if row.get("status") == "active"]
    return {"decisions": [{key: row[key] for key in
             ("id", "domain", "memory_class", "flattering", "accuracy_nonnegative", "report_digest", "scopes")}
             for row in decisions], "classification": "applied fixed-horizon personalization audits",
            "note": "independent grader labels remain an assumption; agreement is not factual truth"}


def benchmark(root: str, name: str, path: str, *, license_notice: str,
              license_confirmed: bool = False) -> dict[str, Any]:
    """Read only an operator-supplied local dataset; never download or execute it."""
    import json
    from pathlib import Path

    _store().operator()
    if license_confirmed is not True:
        raise PermissionError("UniMem review the local dataset license and third-party attribution before use")
    fp.text(license_notice, "operator-reviewed license notice", 2000)
    with Path(path).open("rb") as stream:
        raw = stream.read(fp.MAX_BYTES+1)
    if len(raw) > fp.MAX_BYTES:
        raise ValueError("UniMem benchmark input exceeds 1 MiB; select a bounded local subset")
    values = (json.loads(raw) if raw.lstrip().startswith(b"[")
              else [json.loads(line) for line in raw.splitlines() if line])
    if not isinstance(values, list) or any(not isinstance(row, dict) for row in values):
        raise ValueError("UniMem benchmark requires JSON/JSONL object rows")
    cases = pure.benchmark_cases(name, values)
    return {"cases": cases, "license_notice": license_notice, "classification": "pinned input subset adapter",
            "execution": "no dataset bundled, downloaded or evaluated; provide an independent runner and grader",
            "condition": "registered retrieved memory; gold annotations only supplied to the grader"}


def acceptance(root: str, cases: Sequence[dict[str, Any]], audits: dict[str, str], *,
               license_notice: str, license_confirmed: bool = False,
               runner: Callable[[str, list[dict[str, Any]]], str],
               grader: Callable[[dict[str, Any], str], dict[str, Any]],
               context: Sequence[str] = ()) -> dict[str, Any]:
    """Run actual registered arms through caller-controlled answer and grader hooks.

    Models never receive the grader reference. For MemSyco, the operator must
    construct the audited memory from historical dialogue, independently of gold
    annotations. An adapter cannot prove the independence of external hooks.
    """
    from unimem import holdout_io

    store = _store()
    store.operator()
    if license_confirmed is not True:
        raise PermissionError("UniMem acceptance requires operator-reviewed dataset and source license notices")
    fp.text(license_notice, "operator-reviewed license notice", 2000)
    fp.count(len(cases), 1, 128)
    results = []
    # Validate the entire mapping before any generation or randomized delivery.
    prepared = []
    for case in cases:
        identity = audits[case["id"]]
        value = _load(root, identity, context)
        if (case["benchmark"] not in pure.BENCHMARK_PINS
                or case["source_revision"] != pure.BENCHMARK_PINS[case["benchmark"]]
                or value["design"]["queries"] != [case["id"]]
                or value["design"]["query_texts"][case["id"]] != case["query"]):
            raise ValueError("UniMem acceptance case does not match its pinned preregistered query")
        sources = store.admit(root, value["design"]["identities"], context=context)
        if case["provided_memories"] is not None and sorted(case["provided_memories"]) != sorted(
                source["text"] for source in sources.values()):
            raise PermissionError("UniMem PersistBench memory must match the actual admitted trial catalog")
        prepared.append((case, identity, value))
    for case, identity, value in prepared:
        for index in range(value["design"]["horizon"]):
            occasion = "acceptance:"+identity+":"+str(index)
            if occasion in holdout_io.read_outcomes(root):
                continue
            actual = expose(root, identity, occasion, query_id=case["id"], context=context)
            response = runner(actual["query"], actual["memories"])
            fp.text(response, "observed benchmark response", 20000)
            labels = grader(case["grader_reference"], response)
            if set(labels) != {"succeeded", "accuracy", "agreement"} or type(labels["succeeded"]) is not bool:
                raise ValueError("UniMem grader must return actual task success, accuracy and agreement")
            holdout_io.record_outcome(root, occasion, labels["succeeded"],
                secondary_metrics={name: labels[name] for name in ("accuracy", "agreement")})
        results.append({"case": case["id"], "report": report(root, identity, context=context)})
    return {"results": results, "classification": "actual randomized acceptance harness",
            "assumptions": ["independent runner and grader", "memory built without gold-label access",
                            "operator-reviewed dataset and source-dataset license obligations"]}
