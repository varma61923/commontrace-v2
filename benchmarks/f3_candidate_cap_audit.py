"""Audit whether a smaller dense candidate cap preserves scoped hybrid results.

This is a bounded planner diagnostic, not a latency or relevance benchmark. It
uses a fresh canonical store and a deterministic owner-independent vector
adapter so out-of-scope candidates can deliberately crowd the eligible result.
"""

from __future__ import annotations

import argparse
import json
import tempfile

from unimem import hierarchical, search_recipes


def _alpha(index: int) -> str:
    """Stable alphabetic fixture identifier so numbers do not look contradictory."""
    chars = []
    while index:
        index, remainder = divmod(index, 26)
        chars.append(chr(ord("a") + remainder))
    return "".join(reversed(chars or ["a"]))


def _seed(root: str, count: int, target_rank: int) -> tuple[list[str], str]:
    from unimem import fact_store

    fact_store.migrate(root)
    conflicts: list = []
    admitted = []
    with fact_store.transaction(root, write=True) as facts:
        index = hierarchical._statement_index(facts)
        for rank in range(1, count + 1):
            statement, category, valid_from, valid_until, expires_at = hierarchical.prepare_fact(
                "Synthetic record " + " ".join(
                    _alpha(rank * multiplier) for multiplier in (1, 13, 31, 101, 997)
                ) + ".",
                hierarchical.DEFAULT_CATEGORY,
                None, None, None,
            )
            fact, action = hierarchical._add_locked(
                facts, statement, category,
                ["agent:a"] if rank == target_rank else ["agent:b"],
                valid_from, valid_until, expires_at, 0.8, f"f3-candidate-cap-{rank:05d}", "", None,
                None, 1, index=index, dedupe=hierarchical.dedup_mode(), root=root, conflicts=conflicts,
            )
            if action != "ADD":
                raise RuntimeError("bounded candidate fixture was not uniquely admitted")
            admitted.append(fact)
        hierarchical._batch_conflicts(facts, index, admitted, root, conflicts, may_supersede=True)
    if conflicts:
        raise RuntimeError("bounded candidate fixture unexpectedly generated conflicts")
    identities = [fact.id for fact in admitted]
    return identities, identities[target_rank - 1]


def _search(root: str, ids: list[str], target: str, dense_cap: int) -> list[dict]:
    def vector_search(_query: str, *, limit: int) -> list[tuple[str, float]]:
        return [
            (identity, 0.99 - rank * 0.00001)
            for rank, identity in enumerate(ids[: min(limit, dense_cap)])
        ]

    return search_recipes.search(
        root,
        "celadon",
        embedder="none",
        reranker="none",
        recipe="balanced",
        vector_search=vector_search,
        context=["agent:a"],
        as_of="2030-01-01T00:00:00Z",
        limit=10,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--facts", type=int, default=10_000)
    parser.add_argument("--current-cap", type=int, default=200)
    parser.add_argument("--candidate-cap", type=int, default=50)
    parser.add_argument("--target-rank", type=int, default=61)
    args = parser.parse_args()
    if not 3 <= args.facts <= 10_000:
        parser.error("--facts must be in 3..10000")
    if not 1 <= args.candidate_cap < args.current_cap:
        parser.error("candidate cap must be positive and lower than current cap")
    if not args.candidate_cap < args.target_rank <= min(args.current_cap, args.facts):
        parser.error("target rank must be beyond the candidate cap and within the current cap")

    with tempfile.TemporaryDirectory(prefix="unimem-f3-cap-") as root:
        ids, target = _seed(root, args.facts, args.target_rank)
        baseline = _search(root, ids, target, args.current_cap)
        candidate = _search(root, ids, target, args.candidate_cap)
        baseline_ids = [row["id"] for row in baseline]
        candidate_ids = [row["id"] for row in candidate]
        baseline_scope_safe = all(row["id"] == target for row in baseline)
        candidate_scope_safe = all(row["id"] == target for row in candidate)
        print(json.dumps({
            "fixture_facts": args.facts,
            "current_candidate_cap": args.current_cap,
            "candidate_cap": args.candidate_cap,
            "eligible_dense_target_rank": args.target_rank,
            "baseline_ids": baseline_ids,
            "candidate_ids": candidate_ids,
            "exact_ordered_output_parity": baseline == candidate,
            "baseline_scope_isolated": baseline_scope_safe,
            "candidate_scope_isolated": candidate_scope_safe,
            "decision": "no-go" if baseline != candidate else "parity on this bounded fixture only",
        }, sort_keys=True))
        if not baseline_scope_safe or not candidate_scope_safe or baseline_ids != [target]:
            raise RuntimeError("scope isolation or expected baseline canary failed")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
