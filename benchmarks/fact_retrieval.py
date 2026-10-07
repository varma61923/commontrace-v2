"""Original fact retrieval quality and governance contracts; stdout JSON only.

LongMemEval (https://arxiv.org/abs/2410.10813) separates indexing, retrieval and
reading and tests updates, temporal reasoning and abstention. Hindsight
(https://arxiv.org/abs/2512.12818) distinguishes source evidence from inferred
claims. Those contracts motivate these original labelled examples; this is not
an official benchmark reproduction, downstream answer accuracy, or a competitor
comparison. No answer model, embedding, answer judge or gold-label query expansion runs.

Both scorers read the identical persisted/reopened production fact store and
view options. Labels remain in this evaluator. Ranking regressions are printed,
not hidden or used to cherry-pick a winner. The exit gate checks attribution,
eligibility, erasure and abstention contracts, not a promised quality gain.
Temporary stores are removed after evaluation; no artifact files are produced.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Literal

from commontrace import hierarchical, lesson_cache
from commontrace.fact_evidence import EvidenceResolver, bind_evidence

Scorer = Literal["overlap-v1", "bm25-v1"]
SCORERS: tuple[Scorer, ...] = ("overlap-v1", "bm25-v1")
MAX_K = 1000
MAX_DOCUMENTS = 1000
_EVALUATION_TIME = "2026-06-15T00:00:00Z"


def _identifier(value: str, field: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 200:
        raise ValueError(f"{field} must be a nonempty identifier of at most 200 characters")


def _limit(value: int, field: str, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{field} must be an integer between 1 and {maximum}")


@dataclass(frozen=True)
class RankingMetrics:
    recall_at_k: float | None
    mrr: float | None
    ndcg: float | None

    def __post_init__(self) -> None:
        values = (self.recall_at_k, self.mrr, self.ndcg)
        if any(value is None for value in values) and not all(value is None for value in values):
            raise ValueError("ranking metrics must be either all defined or all undefined")
        for value in values:
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value) or not 0 <= value <= 1):
                raise ValueError("ranking metrics must be finite values within zero to one")

    def to_dict(self) -> dict[str, float | None]:
        return asdict(self)


def ranking_metrics(ranked: Sequence[str], relevant: Mapping[str, int], *, k: int) -> RankingMetrics:
    """Binary recall/MRR and graded nDCG@k; empty gold has undefined metrics.

    Grades are original manual relevance judgments 1..3, not fact confidence.
    Duplicate result identifiers, malformed grades and unbounded inputs fail
    explicitly instead of inflating ranking quality or obscuring bad output.
    """
    _limit(k, "k", MAX_K)
    if isinstance(ranked, (str, bytes)) or not isinstance(ranked, Sequence) or len(ranked) > MAX_DOCUMENTS:
        raise ValueError("ranked must be a bounded sequence of identifiers")
    if not isinstance(relevant, Mapping) or len(relevant) > MAX_DOCUMENTS:
        raise ValueError("relevant must be a bounded mapping of identifiers to grades")
    for identity in ranked:
        _identifier(identity, "ranked identity")
    if len(set(ranked)) != len(ranked):
        raise ValueError("ranked identities must be unique")
    for identity, grade in relevant.items():
        _identifier(identity, "relevant identity")
        if isinstance(grade, bool) or not isinstance(grade, int) or not 1 <= grade <= 3:
            raise ValueError("relevance grades must be integers between 1 and 3")
    if not relevant:
        return RankingMetrics(None, None, None)
    page = ranked[:k]
    matched = sum(identity in relevant for identity in page)
    recall_at_k = matched / len(relevant)
    mrr = next((1.0 / position for position, identity in enumerate(page, 1) if identity in relevant), 0.0)
    dcg = math.fsum((2 ** relevant.get(identity, 0) - 1) / math.log2(position + 1)
                    for position, identity in enumerate(page, 1))
    ideal = math.fsum((2 ** grade - 1) / math.log2(position + 1)
                      for position, grade in enumerate(sorted(relevant.values(), reverse=True)[:k], 1))
    return RankingMetrics(recall_at_k, mrr, min(1.0, max(0.0, dcg / ideal)))


@dataclass(frozen=True)
class Fact:
    key: str
    statement: str
    confidence: float = 0.8
    scopes: tuple[str, ...] = ()
    category: str = "constraint"
    stability: str = ""
    valid_from: str = "2020-01-01T00:00:00Z"
    valid_until: str | None = None
    expires_at: str | None = None
    evidence: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Mutation:
    operation: Literal["forget", "delete", "update", "resolve"]
    key: str
    value: str = ""


@dataclass(frozen=True)
class Case:
    name: str
    ability: str
    query: str
    facts: tuple[Fact, ...]
    relevant: tuple[tuple[str, int], ...]
    forbidden: tuple[str, ...] = ()
    mutations: tuple[Mutation, ...] = ()
    scope: str = ""
    category: str = ""
    stability: str = ""
    as_of: str | None = _EVALUATION_TIME
    expected_abstention: bool = False


def fixtures() -> tuple[Case, ...]:
    """Original cases include lexical benefits and known tokenizer stressors."""
    rare_noise = tuple(Fact(f"noise-{i}", f"Connector {i} timeout budgets follow ordinary operational policy")
                       for i in range(12))
    long_context = " ".join(f"deployment-detail-{i}" for i in range(45))
    long_identifier = "registry_" + "opaque" * 30 + "_z7"
    return (
        Case("rare-connector", "selective-terms", "zircon connector timeout", (
            Fact("critical", "Zircon connector timeout must remain 11 seconds during payment reconciliation"),
            Fact("background", "Zircon connector timeout dashboards are reviewed weekly"), *rare_noise,
        ), (("critical", 3), ("background", 1))),
        Case("technical-field", "selective-terms", "checkout ack_timeout_ms", (
            Fact("field", "Checkout sets ack_timeout_ms to 850 for acknowledgements"),
            Fact("overview", "Checkout acknowledgement timeout settings are documented in the runbook"),
            Fact("other", "The catalogue request timeout is 1200 milliseconds"),
        ), (("field", 3), ("overview", 1))),
        Case("long-source", "length-effects", "lumen cache invalidation", (
            Fact("detailed", f"Lumen cache invalidation runs after every deployment. {long_context}"),
            Fact("cache", "Lumen cache metrics remain available"),
            Fact("other", "Lumen deployment reports retain artifact checksums"),
        ), (("detailed", 3),)),
        Case("graded-context", "graded-relevance", "nimbus token rotation outage", (
            Fact("recovery", "Nimbus outage recovery requires immediate token rotation before reconnecting"),
            Fact("schedule", "Nimbus token rotation normally follows a weekly schedule"),
            Fact("background", "Nimbus outage messages are copied to the operations room"),
            Fact("irrelevant", "A separate gateway displays traffic statistics"),
        ), (("recovery", 3), ("schedule", 2), ("background", 1))),
        Case("english-inflection", "inflection", "retrying requests", (
            Fact("retry", "Retry request failures using exponential backoff"),
            Fact("other", "Backups retain daily snapshots for thirty days"),
        ), (("retry", 3),)),
        Case("accented-terms", "unicode", "café règle", (
            Fact("accent", "La règle du café limite les connexions à huit"),
            Fact("other", "The cafe visitor log closes every evening"),
        ), (("accent", 3),)),
        Case("cjk-retrieval", "cjk", "超时 重试", (
            Fact("cjk", "霓虹数据库超时后应重试连接", confidence=0.7),
            Fact("other", "缓存失效任务每周执行", confidence=0.95),
        ), (("cjk", 3),)),
        Case("sqlstate-identifier", "identifiers", "SQLSTATE 40001 serialization retry", (
            Fact("sqlstate", "Retry serialization transactions when SQLSTATE equals 40001"),
            Fact("permissions", "SQLSTATE 42501 indicates insufficient privilege and should not be retried"),
            Fact("other", "Connection refused errors follow network recovery policy"),
        ), (("sqlstate", 3),)),
        Case("numeric-short-term", "short-terms", "rack 7", (
            Fact("seven", "Rack 7 contains the active billing router", confidence=0.75),
            Fact("eight", "Rack 8 contains the active shipping router", confidence=0.9),
        ), (("seven", 3),)),
        Case("short-identifier", "short-terms", "port x", (
            Fact("x", "Port x carries the telemetry feed", confidence=0.8),
            Fact("y", "Port y carries the diagnostics feed", confidence=0.9),
        ), (("x", 3),)),
        Case("stemming-collision", "stemming-limitations", "university policy", (
            Fact("university", "University policy requires encrypted exports", confidence=0.8),
            Fact("universal", "Universal policy requires default headers", confidence=0.9),
        ), (("university", 3),)),
        Case("long-identifier", "long-terms", long_identifier, (
            Fact("identifier", f"The deployment registry accepts identifier {long_identifier}"),
            Fact("other", "The deployment registry accepts ordinary release names"),
        ), (("identifier", 3),)),
        Case("stopword-technical-term", "stopword-limitations", "IT", (
            Fact("it", "IT handles corporate incident response", confidence=0.8),
            Fact("other", "Shipping handles regional fulfilment", confidence=0.9),
        ), (("it", 3),)),
        Case("tenant-isolation", "scope", "orion private key rotation", (
            Fact("ours", "Orion private key rotation occurs every 18 hours", scopes=("payments",)),
            Fact("theirs", "Orion private key rotation uses SECRET_FINANCE_POLICY", scopes=("finance",), confidence=1),
            Fact("public", "Orion key rotation is governed by the published security policy"),
        ), (("ours", 3), ("public", 1)), forbidden=("theirs",), scope="payments"),
        Case("stable-tier", "stability", "orion deployment policy", (
            Fact("stable", "Orion deployment policy requires two reviewers", stability="stable"),
            Fact("dynamic", "Orion deployment policy currently pauses rollout during an outage", stability="dynamic"),
        ), (("stable", 3),), forbidden=("dynamic",), stability="stable"),
        Case("category-isolation", "category", "orion lease limit", (
            Fact("limit", "Orion lease limit is 32 concurrent writers", category="constraint"),
            Fact("description", "Orion lease limit appears in the operator reference", category="reference", confidence=1),
        ), (("limit", 3),), forbidden=("description",), category="constraint"),
        Case("current-correction", "knowledge-update", "orion shard limit", (
            Fact("old", "Orion shard limit is 16"),
            Fact("new", "Orion shard limit is 64", valid_from="2026-03-01T00:00:00Z"),
        ), (("new", 3),), forbidden=("old",), mutations=(Mutation("resolve", "old", "new"),)),
        Case("historical-correction", "temporal", "orion shard limit", (
            Fact("old", "Orion shard limit was 16"),
            Fact("new", "Orion shard limit is 64", valid_from="2026-03-01T00:00:00Z"),
        ), (("old", 3),), forbidden=("new",), mutations=(Mutation("resolve", "old", "new"),),
             as_of="2025-06-15T00:00:00Z"),
        Case("expiry-and-future", "validity", "orion lease policy", (
            Fact("live", "Orion lease policy requires renewal every 20 minutes"),
            Fact("expired", "Orion lease policy uses EXPIRED_POLICY", expires_at="2026-03-01T00:00:00Z"),
            Fact("future", "Orion lease policy will use FUTURE_POLICY", valid_from="2027-01-01T00:00:00Z"),
        ), (("live", 3),), forbidden=("expired", "future")),
        Case("forgotten-erasure", "erasure", "orion rollback limit", (
            Fact("forgotten", "Orion rollback limit is FORGOTTEN_SECRET", confidence=1),
            Fact("live", "Orion rollback limit is four deployments"),
        ), (("live", 3),), forbidden=("forgotten",), mutations=(Mutation("forget", "forgotten"),)),
        Case("deleted-erasure", "erasure", "orion rollout key", (
            Fact("deleted", "Orion rollout key is DELETED_SECRET", confidence=1),
            Fact("live", "Orion rollout key belongs to the active operations account"),
        ), (("live", 3),), forbidden=("deleted",), mutations=(Mutation("delete", "deleted"),), as_of=None),
        Case("stale-proof-erasure", "source-update", "orion shard limit", (
            Fact("premise", "The measuring device reports the old Orion shard configuration", category="reference"),
            Fact("derived", "Orion shard limit is STALE_DERIVED_64", evidence=("premise",)),
            Fact("live", "Orion shard limit is 42"),
        ), (("live", 3),), forbidden=("derived",), category="constraint",
             mutations=(Mutation("update", "premise", "The measuring device reports a corrected configuration"),)),
        Case("unknown-query", "abstention", "xylophage lunar quartz", (
            Fact("ordinary", "Orion connection pools recycle idle sessions"),
            Fact("other", "Payment reconciliation uses daily summaries"),
        ), (), expected_abstention=True),
    )


def validate_case(case: Case) -> None:
    _identifier(case.name, "case name")
    if not isinstance(case.query, str) or not case.query.strip() or len(case.query) > 10000:
        raise ValueError("case query must be a bounded nonempty string")
    _limit(len(case.facts), "fact count", MAX_DOCUMENTS)
    keys: set[str] = set()
    for fact in case.facts:
        _identifier(fact.key, "fact key")
        if fact.key in keys or not fact.statement or len(fact.statement) > hierarchical.MAX_STATEMENT_CHARS:
            raise ValueError("fact keys must be unique and statements bounded and nonempty")
        if isinstance(fact.confidence, bool) or not isinstance(fact.confidence, (int, float)) \
                or not math.isfinite(fact.confidence) or not 0 <= fact.confidence <= 1:
            raise ValueError("fact confidence must be finite and within zero to one")
        if fact.evidence is not None and not set(fact.evidence) <= keys:
            raise ValueError("evidence must reference previously declared source facts")
        keys.add(fact.key)
    gold = dict(case.relevant)
    if len(gold) != len(case.relevant) or not set(gold) <= keys or not set(case.forbidden) <= keys:
        raise ValueError("relevance and forbidden labels must identify declared facts without duplicates")
    if set(gold) & set(case.forbidden):
        raise ValueError("a relevant fact cannot also be forbidden")
    ranking_metrics([], gold, k=1)
    if case.expected_abstention and gold:
        raise ValueError("an abstention case cannot declare relevant facts")
    for mutation in case.mutations:
        if mutation.operation not in ("forget", "delete", "update", "resolve") \
                or mutation.key not in keys or mutation.operation == "resolve" and mutation.value not in keys:
            raise ValueError("mutation must reference declared facts")
        if mutation.operation == "update" and (not mutation.value or len(mutation.value) > 2000):
            raise ValueError("source correction must contain a bounded nonempty statement")


@dataclass(frozen=True)
class Outcome:
    case: str
    ability: str
    scorer: Scorer
    k: int
    returned: tuple[str, ...]
    missing_relevant: tuple[str, ...]
    metrics: RankingMetrics
    violations: tuple[str, ...]
    eligible_corpus_sha256: str
    error: str | None = None

    @property
    def contracts_passed(self) -> bool:
        return not self.violations and self.error is None

    def to_dict(self) -> dict[str, object]:
        return {"case": self.case, "ability": self.ability, "scorer": self.scorer, "k": self.k,
                "returned": list(self.returned), "missing_relevant": list(self.missing_relevant),
                "metrics": self.metrics.to_dict(), "violations": list(self.violations),
                "eligible_corpus_sha256": self.eligible_corpus_sha256, "error": self.error,
                "contracts_passed": self.contracts_passed}


def _eligible(fact: hierarchical.AtomicFact, case: Case, moment: datetime, resolver: EvidenceResolver) -> bool:
    if fact.forgotten or fact.status == "deleted":
        return False
    if case.as_of is None and fact.status != "active":
        return False
    if case.scope and fact.scopes and case.scope not in fact.scopes:
        return False
    if case.category and case.category != fact.category or case.stability and case.stability != fact.stability:
        return False
    # Independently evaluate date boundaries instead of trusting the search
    # function's selected-candidate eligibility mask.
    if lesson_cache.parse_moment(fact.valid_from) > moment:
        return False
    if fact.valid_until and lesson_cache.parse_moment(fact.valid_until) <= moment:
        return False
    if fact.expires_at and lesson_cache.parse_moment(fact.expires_at) <= moment:
        return False
    return not fact.evidence_bound or resolver.assess(fact.id).eligible


def inspect_result(
    root: str, case: Case, keys: Mapping[str, str], results: Sequence[tuple[hierarchical.AtomicFact, float]],
    *, scorer: Scorer, k: int,
) -> Outcome:
    """Score real returned facts and reject attribution/view/erasure violations."""
    _limit(k, "k", MAX_K)
    if scorer not in SCORERS:
        raise ValueError("unknown scorer")
    facts = hierarchical.load_facts(root)  # Reopen the actual durable store.
    resolver = EvidenceResolver(root, facts, as_of=case.as_of)
    moment = lesson_cache.parse_moment(case.as_of) if case.as_of else datetime.now(timezone.utc)
    eligible = {identity: fact for identity, fact in facts.items() if _eligible(fact, case, moment, resolver)}
    corpus = json.dumps([eligible[identity].to_dict() for identity in sorted(eligible)],
                       ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(corpus.encode()).hexdigest()
    inverse = {identity: key for key, identity in keys.items()}
    gold = {keys[key]: grade for key, grade in case.relevant}
    forbidden = {keys[key] for key in case.forbidden}
    forbidden_statements = {fact.statement for fact in case.facts if fact.key in case.forbidden}
    violations: set[str] = set()
    if not set(keys.values()) <= facts.keys():
        violations.add("durable fixture facts are missing or unreadable")
    if len(results) > k:
        violations.add("result limit exceeded")
    ranked = []
    for fact, score in results:
        ranked.append(fact.id)
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(score) or not 0 <= score <= 1:
            violations.add("nonfinite or unbounded score")
        actual = facts.get(fact.id)
        if actual is None or actual.to_dict() != fact.to_dict():
            violations.add("unknown or changed source attribution")
        if fact.id not in eligible:
            violations.add("ineligible result")
        if fact.id in forbidden:
            violations.add("forbidden or erased fact returned")
        if fact.statement in forbidden_statements:
            violations.add("forbidden fact content returned")
    if case.expected_abstention and ranked:
        violations.add("unknown-query abstention failed")
    try:
        metrics = ranking_metrics(ranked, gold, k=k)
    except ValueError as exc:
        metrics = RankingMetrics(0.0, 0.0, 0.0) if gold else RankingMetrics(None, None, None)
        violations.add(str(exc))
    missing = tuple(sorted(key for key, _grade in case.relevant if keys[key] not in ranked[:k]))
    return Outcome(case.name, case.ability, scorer, k, tuple(inverse.get(identity, "unknown:" + identity)
                                                          for identity in ranked[:MAX_DOCUMENTS]),
                   missing, metrics, tuple(sorted(violations)), digest)


def _search(root: str, case: Case, scorer: Scorer, k: int) -> list[tuple[hierarchical.AtomicFact, float]]:
    return hierarchical.search_facts(root, case.query, scope=case.scope, category=case.category,
                                     stability=case.stability, as_of=case.as_of, limit=k, scorer=scorer)


def execute_case(case: Case, *, k: int = 3, scorers: Sequence[Scorer] = SCORERS) -> tuple[Outcome, ...]:
    """Warm both engines, mutate real source state, then reopen and compare."""
    validate_case(case)
    _limit(k, "k", MAX_K)
    if not scorers or len(set(scorers)) != len(scorers) or any(scorer not in SCORERS for scorer in scorers):
        raise ValueError("scorers must be a nonempty unique selection of supported scorers")
    with tempfile.TemporaryDirectory(prefix="commontrace-fact-quality-") as root:
        keys: dict[str, str] = {}
        try:
            for row in case.facts:
                receipts = ([bind_evidence(root, "fact", keys[key]) for key in row.evidence]
                            if row.evidence is not None else None)
                fact, _ = hierarchical.add_fact(root, row.statement, confidence=row.confidence, scopes=list(row.scopes),
                                                category=row.category, stability=row.stability, valid_from=row.valid_from,
                                                valid_until=row.valid_until, expires_at=row.expires_at, evidence=receipts)
                if fact.id in keys.values():
                    raise ValueError("fixture labels collapsed into one durable fact")
                keys[row.key] = fact.id
        except Exception as exc:
            return tuple(Outcome(case.name, case.ability, scorer, k, (), tuple(key for key, _ in case.relevant),
                                 RankingMetrics(0.0, 0.0, 0.0) if case.relevant else RankingMetrics(None, None, None),
                                 (), "", f"setup failed: {type(exc).__name__}: {exc}") for scorer in scorers)
        warm_errors: dict[Scorer, str] = {}
        for scorer in scorers:
            try:
                _search(root, case, scorer, k)
            except Exception as exc:
                warm_errors[scorer] = f"warm read failed: {type(exc).__name__}: {exc}"
        try:
            for mutation in case.mutations:
                identity = keys[mutation.key]
                if mutation.operation == "forget":
                    hierarchical.forget_fact(root, identity)
                elif mutation.operation == "delete":
                    hierarchical.delete_fact(root, identity)
                elif mutation.operation == "update":
                    hierarchical.update_fact(root, identity, statement=mutation.value)
                elif mutation.operation == "resolve":
                    hierarchical.resolve_contradiction(root, identity, keys[mutation.value])
        except Exception as exc:
            return tuple(Outcome(case.name, case.ability, scorer, k, (), tuple(key for key, _ in case.relevant),
                                 RankingMetrics(0.0, 0.0, 0.0) if case.relevant else RankingMetrics(None, None, None),
                                 (), "", f"mutation failed: {type(exc).__name__}: {exc}") for scorer in scorers)
        outcomes = []
        for scorer in scorers:
            try:
                if scorer in warm_errors:
                    raise RuntimeError(warm_errors[scorer])
                result = inspect_result(root, case, keys, _search(root, case, scorer, k), scorer=scorer, k=k)
            except Exception as exc:
                empty = inspect_result(root, case, keys, [], scorer=scorer, k=k)
                result = Outcome(case.name, case.ability, scorer, k, (), tuple(key for key, _ in case.relevant),
                                 RankingMetrics(0.0, 0.0, 0.0) if case.relevant else empty.metrics,
                                 empty.violations, empty.eligible_corpus_sha256, f"{type(exc).__name__}: {exc}")
            outcomes.append(result)
        if len({outcome.eligible_corpus_sha256 for outcome in outcomes}) != 1:
            return tuple(Outcome(outcome.case, outcome.ability, outcome.scorer, outcome.k, outcome.returned,
                                 outcome.missing_relevant,
                                 RankingMetrics(0.0, 0.0, 0.0) if case.relevant else outcome.metrics,
                                 (*outcome.violations, "paired eligible corpus changed"),
                                 outcome.eligible_corpus_sha256, "scorers did not observe an identical eligible corpus")
                         for outcome in outcomes)
        return tuple(outcomes)


def summarize(outcomes: Sequence[Outcome]) -> dict[str, object]:
    summaries: dict[str, object] = {}
    for scorer in SCORERS:
        selected = [outcome for outcome in outcomes if outcome.scorer == scorer]
        if not selected:
            continue
        values = [outcome.metrics for outcome in selected if outcome.metrics.recall_at_k is not None]
        means = {name: math.fsum(getattr(metric, name) or 0.0 for metric in values) / len(values) if values else None
                 for name in ("recall_at_k", "mrr", "ndcg")}
        summaries[scorer] = {"cases": len(selected), "ranking_cases": len(values),
                             "abstention_cases": len(selected) - len(values), "mean_metrics": means,
                             "contract_failures": sum(not outcome.contracts_passed for outcome in selected),
                             "errors": sum(outcome.error is not None for outcome in selected)}
    by_case: dict[str, dict[Scorer, Outcome]] = {}
    for outcome in outcomes:
        by_case.setdefault(outcome.case, {})[outcome.scorer] = outcome
    wins = ties = losses = 0
    for pair in by_case.values():
        if not all(scorer in pair for scorer in SCORERS):
            continue
        baseline, candidate = (pair[scorer].metrics.ndcg for scorer in SCORERS)
        if baseline is None or candidate is None:
            continue
        if math.isclose(candidate, baseline, abs_tol=1e-12):
            ties += 1
        elif candidate > baseline:
            wins += 1
        else:
            losses += 1
    return {"scorers": summaries, "bm25_ndcg_wins": wins, "bm25_ndcg_ties": ties,
            "bm25_ndcg_losses": losses}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--scorer", choices=("both", *SCORERS), default="both")
    parser.add_argument("--case", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        _limit(args.top_k, "top-k", MAX_K)
    except ValueError as exc:
        parser.error(str(exc))
    available = fixtures()
    selected = tuple(case for case in available if not args.case or case.name in args.case)
    unknown = set(args.case) - {case.name for case in available}
    if unknown:
        parser.error("unknown cases: " + ", ".join(sorted(unknown)))
    scorers = SCORERS if args.scorer == "both" else (args.scorer,)
    outcomes = tuple(outcome for case in selected for outcome in execute_case(case, k=args.top_k, scorers=scorers))
    fixture_digest = hashlib.sha256(json.dumps([asdict(case) for case in selected], ensure_ascii=False,
                                             sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    contracts_passed = all(outcome.contracts_passed for outcome in outcomes)
    manifest = {"schema": 1, "suite": "original-fact-retrieval-v1", "fixture_sha256": fixture_digest,
                "case_count": len(selected), "scorers": list(scorers), "k": args.top_k,
                "contracts_passed": contracts_passed, "outcomes": [outcome.to_dict() for outcome in outcomes],
                "cases": [{"name": case.name, "ability": case.ability, "query": case.query,
                           "view": {"scope": case.scope, "category": case.category, "stability": case.stability,
                                    "as_of": case.as_of}, "relevance": dict(case.relevant),
                           "forbidden": list(case.forbidden), "expected_abstention": case.expected_abstention}
                          for case in selected],
                "comparison": summarize(outcomes), "limitations": [
                    "Original small labelled retrieval examples, not an official benchmark dataset.",
                    "No answer generation, downstream answer accuracy or competitor comparison.",
                    "Ranking metrics may regress; the exit gate requires governance and abstention, not a quality gain.",
                    "Deleted unbound current facts are erased; legacy historical audit semantics are not rewritten.",
                    "The corpus digest includes real write timestamps and is comparable within each paired case.",
                ]}
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0 if contracts_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
