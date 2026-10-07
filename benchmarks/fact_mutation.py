"""Real canonical fact mutations and incremental index reconciliation; stdout only.

LongMemEval (https://arxiv.org/abs/2410.10813) distinguishes updates, temporal
reasoning and retrieval; Zep (https://arxiv.org/abs/2501.13956) preserves validity
windows. These motivate original lifecycle contracts, not a reproduction of
either evaluation, downstream answer accuracy, or a competitor comparison.

The baseline clears retention before each write; the candidate retains its
verified parent generation. Both execute actual locked JSONL transactions.
Every phase compares complete ranked fact dictionaries and exact scores with a
separate canonical full-scan oracle for both scoring profiles. Timings include
canonical load/serialization/write/fsync and any publication/hash verification,
plus the next selective search. Cold indexing, full-result validation and
record-object reuse are reported separately. No timed monkeypatch or timing
threshold is used. Canonical rewrites, verification reads and snapshot map/order
assembly remain O(N); reused records do not imply constant-time mutations.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import statistics
import tempfile
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Literal

from commontrace import _jsonl, fact_index, hierarchical, lesson_cache, retrieval
from commontrace.fact_evidence import EvidenceResolver, bind_evidence, claim_revision

Scorer = Literal["overlap-v1", "bm25-v1"]
Variant = Literal["cold-baseline", "incremental"]
SCORERS: tuple[Scorer, ...] = ("overlap-v1", "bm25-v1")
PHASES = ("add", "statement-update", "metadata-update", "forget", "restore",
          "soft-delete", "erase-source", "temporal-correction", "external-replacement",
          "restart", "rejected-transaction", "no-op")
_START = "2020-01-01T00:00:00+00:00"
_NOW = "2030-06-15T00:00:00Z"
_BEFORE_CORRECTION = "2024-06-15T00:00:00Z"
_CORRECTION = "2025-01-01T00:00:00+00:00"
_TERMS = re.compile(r"[a-z0-9]+")
Rows = list[tuple[hierarchical.AtomicFact, float]]


@dataclass(frozen=True)
class View:
    name: str
    query: str
    scope: str = "alpha"
    as_of: str = _NOW
    category: str = ""
    stability: str = ""
    include_forgotten: bool = False
    show_expired: bool = False


VIEWS = (
    View("selective", "zircon mutation"),
    View("other-tenant", "zircon mutation", scope="beta"),
    View("historical", "zircon mutation", as_of=_BEFORE_CORRECTION),
    View("stable", "zircon mutation", stability="stable"),
    View("category", "zircon mutation", category="constraint"),
    View("forgotten-audit", "zircon mutation", include_forgotten=True),
    View("expired-audit", "zircon mutation", show_expired=True),
    View("full-results", "ordinary zircon mutation"),
)


def _validate_limit(value: int, name: str, low: int, high: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in {low}..{high}")


def checksum(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def rows_checksum(rows: Rows) -> str:
    """Seal all returned fields, exact scores and order, without projection."""
    return checksum([(fact.to_dict(), score) for fact, score in rows])


def full_scan(root: str, view: View, scorer: Scorer) -> Rows:
    """Independent O(N) ranking over canonical rows, without indexed postings.

    Metadata-eligible rows contribute BM25 statistics even when proof admission
    fails, matching the documented production corpus. Proofs are checked afresh
    after ranking. Only the production tokenizer and scalar BM25 formula are
    shared, so this oracle tests incremental data/index coherence, not those
    two lexical primitives. All rows are returned, rather than only top-k.
    """
    if scorer not in SCORERS:
        raise ValueError("unsupported scorer")
    facts = hierarchical.load_facts(root)
    moment = lesson_cache.parse_moment(view.as_of)
    candidates = [fact for fact in facts.values()
                  if (view.include_forgotten or not fact.forgotten)
                  and (not view.scope or not fact.scopes or view.scope in fact.scopes)
                  and (not view.category or fact.category == view.category)
                  and (not view.stability or fact.stability == view.stability)
                  and hierarchical._valid_at(fact, moment)
                  and (view.show_expired or not hierarchical._is_expired(fact, moment))]
    terms = frozenset(_TERMS.findall(view.query.lower())) if scorer == "overlap-v1" \
        else frozenset(fact_index._bm25_tokens(view.query))
    if scorer == "bm25-v1" and not terms:
        return []
    counts = {fact.id: Counter(fact_index._bm25_tokens(fact.statement)) for fact in candidates} \
        if scorer == "bm25-v1" else {}
    frequencies: Counter[str] = Counter()
    for document in counts.values():
        frequencies.update(document.keys())
    average = sum(sum(document.values()) for document in counts.values()) / max(1, len(candidates))
    ranked: Rows = []
    for fact in candidates:
        if not terms:
            ranked.append((fact, fact.confidence))
        elif scorer == "overlap-v1":
            document_terms = frozenset(_TERMS.findall(fact.statement.lower()))
            intersection = terms & document_terms
            if intersection:
                ranked.append((fact, round(len(intersection) / len(terms | document_terms) * .7
                                           + fact.confidence * .3, 4)))
        else:
            document = counts[fact.id]
            if terms & document.keys():
                raw = sum(retrieval._bm25_term(count, retrieval._idf(len(candidates), frequencies[term]),
                                               sum(document.values()), average)
                          for term, count in sorted(document.items()) if term in terms)
                ranked.append((fact, round(raw / (1 + raw) * .7 + fact.confidence * .3, 4)))
    resolver = EvidenceResolver(root, facts, as_of=view.as_of)
    return [(fact, score) for fact, score in sorted(ranked, key=lambda row: (-row[1], row[0].id))
            if not fact.evidence_bound or resolver.assess(fact.id).eligible]


def _search(root: str, view: View, scorer: Scorer, limit: int) -> Rows:
    return hierarchical.search_facts(root, view.query, scope=view.scope, as_of=view.as_of,
                                    category=view.category, stability=view.stability,
                                    include_forgotten=view.include_forgotten, show_expired=view.show_expired,
                                    limit=limit, scorer=scorer)


def _fact(identity: str, statement: str, *, scopes: tuple[str, ...] = ("alpha",),
          confidence: float = .8) -> hierarchical.AtomicFact:
    return hierarchical.AtomicFact(identity, statement, "constraint", list(scopes), confidence, 1,
                                   _START, None, revision="fixture-" + identity, created_at=_START,
                                   updated_at=_START, stability="stable")


def seed(root: str, count: int) -> None:
    """Write original deterministic source facts and a real source-bound claim."""
    _validate_limit(count, "facts", 20, 25000)
    records = {f"ordinary-{i:07}": _fact(f"ordinary-{i:07}",
               f"Ordinary connector component{i} timeout policy is {i % 17 + 1} seconds",
               scopes=("alpha",) if i % 3 else (), confidence=round(.5 + i % 4 * .1, 3))
               for i in range(count - 9)}
    statements = {
        "target": "Zircon mutation target timeout uses six seconds",
        "source": "Zircon mutation evidence source confirms six seconds",
        "erasable-source": "Zircon mutation erasable evidence confirms safe operation",
        "forget-me": "Zircon mutation forgettable connector runs weekly",
        "delete-me": "Zircon mutation deletable connector closes nightly",
        "temporal-old": "Zircon mutation historical timeout uses nine seconds",
        "external": "Zircon mutation external connector uses six seconds",
        "private": "Zircon mutation private beta credential policy stays isolated",
        "expired": "Zircon mutation expired connector belongs to archived operations",
    }
    records.update({identity: _fact(identity, statement, scopes=("beta",) if identity == "private" else ("alpha",))
                    for identity, statement in statements.items()})
    records["expired"].expires_at = "2023-01-01T00:00:00Z"
    hierarchical.save_facts(root, records)
    records = hierarchical.load_facts(root)
    for identity, source in (("derived", "erasable-source"), ("stale-derived", "source")):
        receipt = replace(bind_evidence(root, "fact", source), recorded_at=_START)
        derived = _fact(identity, f"Zircon mutation {identity} connector may operate safely")
        derived.evidence_bound = True
        derived.evidence = [receipt]
        derived.evidence_revision = claim_revision(derived)
        records[derived.id] = derived
    hierarchical.save_facts(root, records)


class _RejectedTransaction(Exception):
    pass


def mutate(root: str, phase: str) -> None:
    """Actual durable JSONL mutations; fixed timestamps make paired data identical.

    The public transactional map supports deterministic batch fixture mutations.
    External replacement intentionally bypasses index publication. Restart
    means a fresh in-process index, not a claim to measure process startup.
    """
    if phase not in PHASES:
        raise ValueError("unknown mutation phase")
    if phase == "restart":
        fact_index.clear_cache()
        return
    if phase == "external-replacement":
        records = hierarchical.load_facts(root)
        # Same byte length and unchanged revision deliberately defeat size-only
        # or revision-only caches; the canonical generation still must change.
        records["external"].statement = records["external"].statement.replace("six", "ten")
        _jsonl.write_rows(hierarchical._facts_file(root), (fact.to_dict() for fact in records.values()))
        return
    try:
        with hierarchical.mutate_facts(root) as facts:
            if phase == "add":
                facts["added"] = _fact("added", "Zircon mutation added connector now uses eleven seconds")
            elif phase == "statement-update":
                facts["target"].statement = "Zircon mutation target timeout now uses twelve seconds"
                facts["target"].revision = "fixture-target-changed"
                facts["target"].updated_at = _CORRECTION
                facts["source"].statement = "Zircon mutation evidence source now contradicts six seconds"
            elif phase == "metadata-update":
                facts["target"].confidence = .35
                facts["target"].stability = "dynamic"
            elif phase in ("forget", "restore"):
                facts["forget-me"].forgotten = phase == "forget"
            elif phase == "soft-delete":
                facts["delete-me"].status = "deleted"
                facts["delete-me"].valid_until = _CORRECTION
            elif phase == "erase-source":
                del facts["erasable-source"]
            elif phase == "temporal-correction":
                facts["temporal-old"].valid_until = _CORRECTION
                facts["temporal-old"].status = "superseded"
                facts["temporal-old"].superseded_by = "temporal-new"
                replacement = _fact("temporal-new", "Zircon mutation corrected timeout uses thirteen seconds")
                replacement.valid_from = _CORRECTION
                facts[replacement.id] = replacement
            elif phase == "rejected-transaction":
                facts["target"].statement = "Zircon mutation uncommitted corrupt target"
                raise _RejectedTransaction
            # no-op commits identical bytes with a new atomic file generation.
    except _RejectedTransaction:
        return


def _objects(root: str) -> Mapping[str, object]:
    return dict(fact_index.snapshot_facts(root)._snapshot.records)


@dataclass(frozen=True)
class Observation:
    trial: int
    variant: Variant
    phase: str
    canonical_mutation_ms: float
    next_selective_search_ms: float
    mutation_and_search_ms: float
    oracle_validation_ms: float
    canonical_bytes: int
    corpus_sha256: str
    results_sha256: str
    records_before: int
    records_after: int
    reused_record_objects: int
    constructed_record_objects: int
    added_records: int
    removed_records: int
    proof_bound_records: int
    eligible_proof_bound_records: int
    errors: tuple[str, ...]


def validate(root: str, phase: str, limit: int) -> tuple[str, tuple[str, ...]]:
    """Full result oracle plus explicit isolation/erasure/temporal assertions."""
    outputs: dict[str, str] = {}
    errors: list[str] = []
    index = PHASES.index(phase)
    for scorer in SCORERS:
        for view in VIEWS:
            actual, expected = _search(root, view, scorer, limit), full_scan(root, view, scorer)
            outputs[f"{scorer}/{view.name}"] = rows_checksum(actual)
            if actual != expected:
                errors.append(f"{scorer}/{view.name}: full fact fields, ordering or scores differ from canonical scan")
            identities = {fact.id for fact, _score in actual}
            if view.scope == "alpha" and "private" in identities:
                errors.append(f"{scorer}/{view.name}: private beta fact leaked")
            if view.scope == "beta" and identities != {"private"}:
                errors.append(f"{scorer}/{view.name}: scoped corpus changed")
            if index >= PHASES.index("erase-source") and "derived" in identities:
                errors.append(f"{scorer}/{view.name}: erased premise left derived claim eligible")
            if index >= PHASES.index("statement-update") and "stale-derived" in identities:
                errors.append(f"{scorer}/{view.name}: corrected premise left stale derived claim eligible")
            if view.scope == "alpha" and index < PHASES.index("erase-source") and "derived" not in identities:
                errors.append(f"{scorer}/{view.name}: initially valid derived claim was never admitted")
            if phase == "add" and view.scope == "alpha" and "stale-derived" not in identities:
                errors.append(f"{scorer}/{view.name}: initially valid correction-bound claim was never admitted")
            if phase == "forget" and not view.include_forgotten and "forget-me" in identities:
                errors.append(f"{scorer}/{view.name}: forgotten fact leaked")
            if not view.show_expired and "expired" in identities:
                errors.append(f"{scorer}/{view.name}: expired fact leaked")
            if index >= PHASES.index("temporal-correction") and view.scope == "alpha":
                historical = view.as_of == _BEFORE_CORRECTION
                if ("temporal-old" in identities) != historical or ("temporal-new" in identities) == historical:
                    errors.append(f"{scorer}/{view.name}: validity correction is inconsistent")
    return checksum(outputs), tuple(errors)


def _series(root: str, count: int, trial: int, variant: Variant) -> tuple[float, str, list[Observation]]:
    seed(root, count)
    fixture_hash = checksum({key: value.to_dict() for key, value in hierarchical.load_facts(root).items()})
    fact_index.clear_cache()
    started = time.perf_counter_ns()
    _search(root, VIEWS[0], "overlap-v1", count + 10)
    cold_ms = (time.perf_counter_ns() - started) / 1e6
    observations: list[Observation] = []
    for phase in PHASES:
        before = _objects(root)
        if variant == "cold-baseline":
            fact_index.clear_cache()
        started = time.perf_counter_ns()
        mutate(root, phase)
        written = time.perf_counter_ns()
        _search(root, VIEWS[0], "overlap-v1", count + 10)
        searched = time.perf_counter_ns()
        after = _objects(root)
        verified_at = time.perf_counter_ns()
        result_hash, errors = validate(root, phase, count + 10)
        verified_ms = (time.perf_counter_ns() - verified_at) / 1e6
        records = hierarchical.load_facts(root)
        resolver = EvidenceResolver(root, records, as_of=_NOW)
        bound = [fact for fact in records.values() if fact.evidence_bound]
        reused = sum(identity in before and before[identity] is record for identity, record in after.items())
        observations.append(Observation(
            trial, variant, phase, (written - started) / 1e6, (searched - written) / 1e6,
            (searched - started) / 1e6, verified_ms, os.path.getsize(hierarchical._facts_file(root)),
            checksum({key: value.to_dict() for key, value in records.items()}), result_hash,
            len(before), len(after), reused, len(after) - reused, len(after.keys() - before.keys()),
            len(before.keys() - after.keys()), len(bound), sum(resolver.assess(fact.id).eligible for fact in bound), errors))
    return cold_ms, fixture_hash, observations


def measure(*, facts: int = 500, trials: int = 3) -> dict[str, object]:
    """Compare identical lifecycle data, retaining all raw samples and checksums."""
    _validate_limit(facts, "facts", 20, 25000)
    _validate_limit(trials, "trials", 1, 20)
    observations: list[Observation] = []
    cold: list[float] = []
    fixture_hashes: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="commontrace-fact-mutation-") as directory:
            for trial in range(trials):
                variants: tuple[Variant, ...] = ("cold-baseline", "incremental") if trial % 2 == 0 \
                    else ("incremental", "cold-baseline")
                for variant in variants:
                    root = os.path.join(directory, f"{trial}-{variant}")
                    initial_ms, fixture_hash, samples = _series(root, facts, trial, variant)
                    cold.append(initial_ms)
                    fixture_hashes.append(fixture_hash)
                    observations.extend(samples)
    finally:
        fact_index.clear_cache()
    errors = [error for sample in observations for error in sample.errors]
    if len(set(fixture_hashes)) != 1:
        errors.append("initial original fixtures differ across paired roots/trials")
    summaries: list[dict[str, object]] = []
    for phase in PHASES:
        pair = [sample for sample in observations if sample.phase == phase]
        if len({sample.corpus_sha256 for sample in pair}) != 1 or len({sample.results_sha256 for sample in pair}) != 1:
            errors.append(f"{phase}: paired full corpus/result checksums differ")
        summary: dict[str, object] = {"phase": phase}
        for variant in ("cold-baseline", "incremental"):
            values = [sample for sample in pair if sample.variant == variant]
            summary[variant] = {
                "canonical_mutation_median_ms": statistics.median(sample.canonical_mutation_ms for sample in values),
                "next_selective_search_median_ms": statistics.median(sample.next_selective_search_ms for sample in values),
                "mutation_and_search_median_ms": statistics.median(sample.mutation_and_search_ms for sample in values),
                "constructed_record_objects": [sample.constructed_record_objects for sample in values],
                "reused_record_objects": [sample.reused_record_objects for sample in values],
            }
        summaries.append(summary)
    if any(not math.isfinite(sample.mutation_and_search_ms) or sample.mutation_and_search_ms < 0
           for sample in observations):
        errors.append("invalid timing sample")
    return {"benchmark": "fact-mutation-v1", "python": platform.python_version(),
            "facts_requested": facts, "initial_persisted_facts": facts + 2, "trials": trials,
            "initial_fixture_sha256": fixture_hashes[0],
            "scorers": SCORERS, "views": [asdict(view) for view in VIEWS],
            "cold_selective_search_ms": cold, "cold_selective_search_median_ms": statistics.median(cold),
            "timed_search_scorer": "overlap-v1", "oracle": "canonical full scan, complete fact dictionaries and exact scores",
            "remaining_linear_costs": ["canonical read and JSONL rewrite/fsync", "publication SHA256 verification read",
                                       "immutable record/posting map and confidence/time ordering assembly"],
            "record_counter": "object identity outside timings; includes metadata refresh, not a tokenizer call count",
            "restart": "in-process index cleared; process startup is not timed",
            "contracts_passed": not errors, "errors": errors, "summaries": summaries,
            "samples": [asdict(sample) for sample in observations]}


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--facts", type=int, default=500)
    parser.add_argument("--trials", type=int, default=3)
    args = parser.parse_args(argv)
    try:
        output = measure(facts=args.facts, trials=args.trials)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    if not output["contracts_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
