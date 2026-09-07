"""Does the HUB find the right memory when a task is described in the
operator's own words?

STRATEGY.md §13.2 calls this link 2 and marks it *"measured, holds"*, citing
84.8% recall@1 / 95.7%@5 / 97.8% findable. §13.3 then says link 2 plus §12.3
is the one thing that makes this more than a good DevTools business.

Those numbers are real. They were measured by `commons/eval/retrieval_tiers.py`
against `commontrace/retrieval.py:rank_lessons` -- the **local, file-based
tier**. Nothing has ever measured `hub/crud.py:search_traces`, which is the
path every Hub customer's agent actually calls, and the two tiers do not
merely differ in implementation. They combine query terms with opposite
boolean operators:

    local tier   weighted token OVERLAP, sorted, top-k, no threshold.
                 §12.7: "any single shared content word puts a lesson on
                 the list."

    Hub          plainto_tsquery(), which ANDs every lexeme:
                 'custom' & 'charg' & 'twice' & 'one' & 'order' & ...
                 A trace must contain ALL of them or it does not match.

So §12.7's sentence -- the sentence link 2 rests on -- is a true statement
about the tier that is not being sold. This module measures the tier that is,
on the same corpus and the same probes, so the comparison is like for like.

WHY THE FAILURE IS WORTH MEASURING RATHER THAN REASONING ABOUT
--------------------------------------------------------------
A conjunctive miss does not raise. `search_traces` returns
`{"traces": [], ...}`, HTTP 200, and the agent correctly concludes there is
no relevant prior experience and proceeds without it. The customer sees a
product that "has no memory of that yet", which is indistinguishable from an
empty corpus. Nothing in the Hub's telemetry separates the two: `retrievals`
counts rows returned, so a query that matched nothing increments nothing and
leaves no trace of having been asked.

That silence is why this compounds into §19's holdout. The randomized
experiment measures the effect of *injecting retrieved memory*. If retrieval
returns nothing, both arms get nothing, the measured effect is zero, and the
honest reading of a null result -- "per-org memory does not deliver
measurable value", §13.2 link 1's falsifier firing -- would be drawn about a
product whose retrieval never fired. The cheapest falsifier in the document
would return a false negative.

WHAT IS REPORTED
----------------
    recall@k          fraction of held-out positives whose target trace is
                      in the top k. The headline §12.7 quotes.
    findable          target present anywhere in the returned list.
    zero-result rate  fraction of positives for which the Hub returned
                      NOTHING AT ALL. This is the number this file exists
                      for; the local tier's is 0% by construction.
    MRR               mean reciprocal rank, which unlike recall@k notices
                      when the answer moves from rank 1 to rank 4.
    controls          the 22 failures deliberately absent from the corpus.
                      A retrieval tier with no threshold returns something
                      for these by design (§12.7: that is the trade, and
                      the cost is a glance) -- reported so that any recall
                      gain can be read against what it cost in noise.

WHAT IT DOES NOT MEASURE
------------------------
Everything `commons/eval/run.py`'s docstring already caveats, and it applies
unchanged: the same author wrote corpus and probes, so shared conceptual
framing survives paraphrase and these numbers are an OPTIMISTIC bound on a
real fleet. What transfers is not the level but the DIFFERENCE between two
tiers measured on identical inputs -- author bias inflates both equally.

Nor is it a latency benchmark; that is `hub/bench_scaling.py`.

Usage:
    python -m hub.bench_retrieval                  # probes-v1 (dev set)
    python -m hub.bench_retrieval --probes v2      # held-out set
    python -m hub.bench_retrieval --probes all
    python -m hub.bench_retrieval --distractors 5000
    python -m hub.bench_retrieval --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

from sqlalchemy import text

from hub import crud
from hub.config import HubConfig
from hub.db import make_engine, make_session_factory, session_scope
from hub.models import Base, Organization, Trace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from commontrace import retrieval  # noqa: E402

CORPUS = ROOT / "commons" / "seed" / "substrate-v1.jsonl"
EVAL_DIR = ROOT / "commons" / "eval"
PROBE_SETS = {"v1": EVAL_DIR / "probes-v1.jsonl", "v2": EVAL_DIR / "probes-v2.jsonl"}

# The k values §12.7 reports, so the two tiers can be read off the same row.
KS = (1, 3, 5, 10)

# Deep enough that "findable" means findable, shallow enough that it is
# still a list somebody could skim. The corpus is 46 records; asking for
# more than it holds would make findable trivially 100%.
FINDABLE_K = 25


def _load(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def probe_query(p: dict) -> str:
    """Exactly what commons/eval/retrieval_tiers.py feeds the local tier.
    A different query string here would measure a different question."""
    return f"{p['label']} {p.get('text', '')}"


def as_lessons(corpus: list[dict]) -> list[tuple[str, dict]]:
    """The local tier's own shape, copied from retrieval_tiers.py so the two
    files cannot drift into measuring different corpora."""
    return [
        (
            r["title"],
            {
                "name": r["title"],
                "description": r["title"],
                "applies_when": r.get("context_text", ""),
                "tags": r.get("tags") or [],
                "domain": "",
            },
        )
        for r in corpus
    ]


async def _seed_corpus(session, org_id: str, corpus: list[dict]) -> None:
    """ORM inserts, not bench_scaling's generate_series: 46 rows whose exact
    wording IS the measurement. `search_vector` is a GENERATED column, so
    Postgres builds it on insert and nothing here can forget to.

    Deliberately does not maintain Organization.trace_count (unlike every
    production insert path -- crud.contribute_trace/amend_trace/
    review_kb_submission, manage.commons_seed): this script measures
    search_traces against a disposable benchmark database/org, never
    plan.max_traces enforcement, so there is nothing here that reads that
    counter back. Do not seed benchmark data into a real customer org for
    this reason -- its trace_count would silently under-report afterward.
    """
    for r in corpus:
        session.add(
            Trace(
                org_id=org_id,
                title=r["title"],
                context_text=r.get("context_text", ""),
                solution_text=r.get("solution_text", ""),
                tags=list(r.get("tags") or []),
                agent_type=r.get("agent_type", ""),
                contributor="bench",
            )
        )
    await session.flush()


async def _seed_distractors(session, org_id: str, n: int) -> None:
    """Unrelated rows, to answer the question a 46-record corpus cannot:
    does relaxing the boolean operator drown the answer once a real fleet's
    corpus is around it? Deliberately drawn from the same *domain* vocabulary
    (production incidents) rather than random words -- distractors that share
    no vocabulary with the probes would make any relaxation look free."""
    await session.execute(
        text(
            """
            INSERT INTO traces (
                id, org_id, title, context_text, solution_text, tags, agent_type,
                agent_id, profile, extensions, watch_condition, review_after,
                contributor, created_at, outcome, trust, retrievals, depth,
                quarantined, quarantine_reason, shared_with_commons,
                shared_rationale, commons_hits, commons_source, commons_votes,
                commons_retraction_reason
            )
            SELECT
                gen_random_uuid(), :org_id,
                (ARRAY[
                    'deploy rolled back after a failed health check',
                    'nightly report job exceeded its memory limit',
                    'customer support macro sent the wrong template',
                    'feature flag evaluated inconsistently across regions',
                    'log volume spiked after a verbose library upgrade',
                    'staging database restored over the wrong snapshot',
                    'certificate renewal missed a wildcard subdomain',
                    'search index rebuild ran during peak traffic',
                    'onboarding email queued but never delivered',
                    'invoice PDF rendered with a stale currency symbol',
                    'metrics dashboard showed a gap during a collector restart',
                    'CI runner ran out of disk mid-build'
                ])[1 + (i % 12)] || ' (incident ' || i || ')',
                'reported by an on-call engineer during run ' || i
                || ' while the service was otherwise healthy',
                'triaged, mitigated, and written up in the incident channel',
                ARRAY['ops', 'incident', 'run' || (i % 40)],
                (ARRAY['support','code','sales'])[1 + (i % 3)],
                'agent-' || (i % 25), '', '{}'::jsonb, '', '', 'bench',
                now() - (i || ' minutes')::interval,
                '{}'::jsonb, 0.5, 0, 0, false, '', false, '', 0, 'org', 0, ''
            FROM generate_series(1, CAST(:n AS bigint)) AS i
            """
        ),
        {"org_id": org_id, "n": n},
    )


def _rank_of(target: str, titles: list[str]) -> int | None:
    """1-indexed position of the target title, or None if absent."""
    for i, t in enumerate(titles, start=1):
        if t == target:
            return i
    return None


def _summarize(ranks: list[int | None], n_returned: list[int]) -> dict:
    """`ranks` is one entry per positive probe: the 1-indexed rank of its
    target, or None. `n_returned` is how many results that probe got at all.

    zero_result is computed from n_returned rather than from ranks, because
    "the right answer was not in the list" and "there was no list" are
    different failures and only the second one is invisible to a user.
    """
    n = len(ranks)
    if n == 0:
        return {"n": 0}
    found = [r for r in ranks if r is not None]
    return {
        "n": n,
        "recall": {k: sum(1 for r in found if r <= k) / n for k in KS},
        "findable": len(found) / n,
        "zero_result": sum(1 for c in n_returned if c == 0) / n,
        "mrr": sum(1.0 / r for r in found) / n,
    }


async def _hub_positives(session, org_id: str, pos: list[dict]) -> dict:
    ranks: list[int | None] = []
    counts: list[int] = []
    for p in pos:
        res = await crud.search_traces(session, org_id, query=probe_query(p), limit=FINDABLE_K)
        titles = [t["title"] for t in res["traces"]]
        counts.append(len(titles))
        ranks.append(_rank_of(p["target"], titles))
    return _summarize(ranks, counts)


async def _hub_controls(session, org_id: str, neg: list[dict]) -> dict:
    """For the negative controls there is no target, so the only question is
    how much comes back. Reported at the same k values §12.7 uses."""
    out: dict[int, float] = {}
    for k in (1, 3, 5):
        hits = 0
        for p in neg:
            res = await crud.search_traces(session, org_id, query=probe_query(p), limit=k)
            if res["traces"]:
                hits += 1
        out[k] = hits / len(neg) if neg else 0.0
    return {"n": len(neg), "returns_something": out}


def _local(corpus: list[dict], pos: list[dict]) -> dict:
    lessons = as_lessons(corpus)
    ranks: list[int | None] = []
    counts: list[int] = []
    for p in pos:
        ranked = retrieval.rank_lessons(probe_query(p), lessons, top_k=FINDABLE_K)
        titles = [r.slug for r in ranked]
        counts.append(len(titles))
        ranks.append(_rank_of(p["target"], titles))
    return _summarize(ranks, counts)


async def evaluate(probe_names: list[str], distractors: int) -> dict:
    config = HubConfig.from_env()
    engine = make_engine(config)
    session_factory = make_session_factory(engine)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text('CREATE EXTENSION IF NOT EXISTS "pgcrypto"'))

    corpus = _load(CORPUS)
    marker = f"retrieval-bench-{uuid.uuid4().hex[:8]}"
    async with session_scope(session_factory) as session:
        org = Organization(name=marker)
        session.add(org)
        await session.flush()
        org_id = org.id
        await _seed_corpus(session, org_id, corpus)
        if distractors:
            await _seed_distractors(session, org_id, distractors)

    async with engine.begin() as conn:
        await conn.execute(text("ANALYZE traces"))

    sets = {}
    try:
        for name in probe_names:
            probes = _load(PROBE_SETS[name])
            pos = [p for p in probes if p["expect"] == "covered"]
            neg = [p for p in probes if p["expect"] == "uncovered"]
            async with session_scope(session_factory) as session:
                hub = await _hub_positives(session, org_id, pos)
                controls = await _hub_controls(session, org_id, neg)
            sets[name] = {"hub": hub, "hub_controls": controls, "local": _local(corpus, pos)}
    finally:
        async with session_scope(session_factory) as session:
            planted = await session.get(Organization, org_id)
            if planted is not None:
                await session.delete(planted)
        await engine.dispose()

    return {
        "n_corpus": len(corpus),
        "distractors": distractors,
        "findable_k": FINDABLE_K,
        "probe_sets": sets,
    }


def _print(report: dict) -> None:
    total = report["n_corpus"] + report["distractors"]
    print(
        f"\ncorpus {report['n_corpus']} substrate records"
        + (f" + {report['distractors']:,} distractors = {total:,} traces" if report["distractors"] else "")
        + f"   (findable = top {report['findable_k']})\n"
    )
    for name, r in report["probe_sets"].items():
        hub, loc, ctl = r["hub"], r["local"], r["hub_controls"]
        print(f"probes-{name}: {hub['n']} held-out positives, {ctl['n']} negative controls")
        head = f"  {'':<22} {'HUB (search_traces)':>20} {'local (rank_lessons)':>22}"
        print(head)
        print("  " + "-" * (len(head) - 2))
        for k in KS:
            print(f"  {'recall@' + str(k):<22} {hub['recall'][k]:>19.1%} {loc['recall'][k]:>21.1%}")
        print(f"  {'findable':<22} {hub['findable']:>19.1%} {loc['findable']:>21.1%}")
        print(f"  {'MRR':<22} {hub['mrr']:>19.3f} {loc['mrr']:>21.3f}")
        print(f"  {'RETURNED NOTHING':<22} {hub['zero_result']:>19.1%} {loc['zero_result']:>21.1%}")
        print()
        print("  negative controls that still return something (the cost of no threshold):")
        for k, v in ctl["returns_something"].items():
            print(f"    top_k={k:<19} {v:>19.1%}")
        print()

    worst = max(r["hub"]["zero_result"] for r in report["probe_sets"].values())
    if worst > 0.0:
        print(
            f"On up to {worst:.0%} of held-out positives the Hub returned NOTHING. That is not a\n"
            "low-quality answer, it is no answer, and it is indistinguishable from an empty\n"
            "corpus to the agent, to the customer, and to §19's holdout -- which measures the\n"
            "effect of injecting memory and would score both arms identically."
        )
    else:
        print("The Hub returned at least one candidate for every held-out positive.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m hub.bench_retrieval", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("--probes", default="v1", choices=["v1", "v2", "all"])
    parser.add_argument(
        "--distractors",
        type=int,
        default=0,
        help="unrelated traces to seed alongside the corpus (default: %(default)s)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    if args.distractors < 0:
        print("error: --distractors must be >= 0", file=sys.stderr)
        return 2
    names = ["v1", "v2"] if args.probes == "all" else [args.probes]
    report = asyncio.run(evaluate(names, args.distractors))
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
