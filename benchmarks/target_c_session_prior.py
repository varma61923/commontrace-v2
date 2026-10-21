"""Benchmark a session-prior reranking diagnostic without changing product defaults.

This runner reorders only the public recall candidate list. It is intended for
bounded LongMemEval retrieval experiments, not production use.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import math
import sys

from benchmarks import conversation_bench
from unimem.conversation import search, timeparse


def _session_prior_order(store, ranked: list[int], alpha: float) -> list[int]:
    """Add a scaled first-hit session prior to each candidate's reciprocal rank."""
    if alpha <= 0 or len(ranked) < 2:
        return ranked
    turns = store.turns(ranked)
    first_by_session: dict[str, int] = {}
    for rank, turn_id in enumerate(ranked):
        turn = turns.get(turn_id)
        if turn is not None:
            first_by_session.setdefault(turn.session, rank)
    scored = []
    for rank, turn_id in enumerate(ranked):
        turn = turns.get(turn_id)
        first = first_by_session.get(turn.session, rank) if turn is not None else rank
        score = 1.0 / (61.0 + rank) + alpha / (61.0 + first)
        scored.append((score, rank, turn_id))
    return [turn_id for _score, _rank, turn_id in sorted(scored, key=lambda row: (-row[0], row[1]))]


def _rerank(original, alpha: float):
    def wrapped(store, question, *, now=None, options=None, extra_queries=(), dense_candidates=None):
        result = original(store, question, now=now, options=options,
                          extra_queries=extra_queries, dense_candidates=dense_candidates)
        candidate = _session_prior_order(store, result.ranked, alpha)
        if candidate == result.ranked:
            return result

        opts = options or search.Options()
        moment = timeparse.parse_moment(now) if isinstance(now, str) else now
        if moment is not None:
            moment = moment.replace(tzinfo=None)
        moment = moment or store.latest_moment()
        until = opts.until
        if now is not None and moment is not None:
            parsed_until = timeparse.parse_moment(opts.until) if opts.until else moment
            until = min(parsed_until, moment)
        allowed = store.allowed(sessions=opts.sessions, speakers=opts.speakers, since=opts.since,
                                until=until, now=moment if now is not None else None)
        if allowed is not None and len(allowed) == store.turn_count():
            allowed = None
        show_now = moment if now is not None else None
        belief_at = moment if now is not None else None
        if belief_at is None and result.window is not None and moment is not None \
                and result.window[1] < moment.date():
            belief_at = dt.datetime.combine(result.window[1], dt.time(23, 59, 59))
        context, used, tokens = search.assemble(
            store, question, candidate, opts, [], allowed, now=show_now, as_of=belief_at,
            current_instructions=now is None and belief_at is not None,
            evidence_paths=result.explain.get("graph_paths", ()),
        )
        explain = dict(result.explain)
        explain["experimental_session_prior_alpha"] = alpha
        return dataclasses.replace(result, context=context, turns=used, tokens=tokens,
                                   ranked=candidate, explain=explain)

    return wrapped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-prior-alpha", type=float, required=True,
                        help="0 runs the unchanged baseline; positive values add a session prior")
    args, benchmark_args = parser.parse_known_args(argv)
    if not math.isfinite(args.session_prior_alpha) or args.session_prior_alpha < 0:
        parser.error("--session-prior-alpha must be finite and nonnegative")
    if args.session_prior_alpha:
        search._recall = _rerank(search._recall, args.session_prior_alpha)
    sys.argv = ["conversation_bench", *benchmark_args]
    return conversation_bench.main()


if __name__ == "__main__":
    raise SystemExit(main())
