"""Compare deterministic retrieval policies; no model quality or latency claims.

Run this harness from the candidate checkout with an unchanged baseline source:
python -m benchmarks.hybrid_evidence --source /baseline --source /candidate --output /tmp/evidence.json
Each source runs in its own isolated Python process. Dense scores and query
blocks are synthetic fixtures; canonical admission uses real temporary SQLite.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from typing import Any


def fingerprint(source: str) -> dict[str, str]:
    result = subprocess.run(["git", "ls-files", "-z", "--", "commontrace"], cwd=source,
                            capture_output=True, check=True)
    hashes = {}
    for relative in result.stdout.decode().split("\0"):
        if relative:
            with open(os.path.join(source, relative), "rb") as stream:
                hashes[relative] = hashlib.sha256(stream.read()).hexdigest()
    return hashes


def worker(source: str) -> dict[str, Any]:
    sys.path.insert(0, source)
    from commontrace import fact_store, hierarchical, search_recipes

    before = fingerprint(source)
    modules = {}
    for name, module in tuple(sys.modules.items()):
        if name == "commontrace" or name.startswith("commontrace."):
            filename = os.path.abspath(module.__file__)
            if os.path.commonpath([source, filename]) != source:
                raise RuntimeError("runtime import escaped the requested source: " + name)
            modules[name] = filename

    class Backend:
        def __init__(self, edges):
            self.edges = edges

        def neighbors(self, node_id, *, as_of=None):
            return [{"neighbor_id": identity} for identity in self.edges.get(node_id, ())]

    measured = {}
    with tempfile.TemporaryDirectory(prefix="commontrace-hybrid-evidence-") as directory:
        root = os.path.join(directory, "dense-graph")
        fact_store.migrate(root)
        anchor, _ = hierarchical.add_fact(root, "Grammar boundaries need validation", scopes=["agent:a"])
        support, _ = hierarchical.add_fact(root, "Use a bounded syntax tree before execution", scopes=["agent:a"])
        private, _ = hierarchical.add_fact(root, "Hidden private account details", scopes=["agent:b"])
        backend = Backend({"memory:" + anchor.id: ["memory:" + support.id, "memory:" + private.id]})
        rows = search_recipes.search(root, "parser", context=["agent:a"], embedder="none", reranker="none",
            vector_search=lambda query, limit: [(anchor.id, .9)], backend=backend)
        identities = [row["id"] for row in rows]
        measured["dense_graph"] = {"eligible_support_found": support.id in identities,
            "private_fact_returned": private.id in identities, "returned_count": len(rows)}

        def populate(root, identities):
            fact_store.migrate(root)
            now = hierarchical._now()
            with fact_store.transaction(root, write=True) as facts:
                for identity in identities:
                    facts[identity] = hierarchical.AtomicFact(identity, identity, "general", [], .8, 1, now, None,
                        created_at=now, updated_at=now)

        original = search_recipes.search
        root = os.path.join(directory, "facets")
        populate(root, ["common-a", "common-b", "one", "two"])
        outputs = {"question": ["common-a", "common-b"], "facet-one": ["one", "common-a", "common-b"],
                   "facet-two": ["two", "common-a", "common-b"]}
        decisions = iter([{"grade": "PARTIAL", "queries": ["facet-one", "facet-two"]},
                          {"grade": "COMPLETE", "queries": []}])

        def retrieve(root, query, **options):
            return [{"id": identity, "text": identity, "score": 1} for identity in outputs[query]]

        search_recipes.search = retrieve
        try:
            result = search_recipes.multi_round(root, "question", limit=3,
                complete=lambda prompt: (json.dumps(next(decisions)), {}))
        finally:
            search_recipes.search = original
        identities = [row["id"] for row in result["results"]]
        measured["distinct_facets"] = {"returned_ids": identities,
            "facets_found": len({"one", "two"}.intersection(identities)), "facet_count": 2}

        root = os.path.join(directory, "exhaustion")
        populate(root, ["common", "core", "facet"])
        calls: dict[str, Any] = {"search_queries": [], "judge_invocations": 0}

        def repeated_retrieve(root, query, **options):
            calls["search_queries"].append(query)
            identities = ["common", "core"] if query == "question" else ["facet"]
            return [{"id": identity, "text": identity, "score": 1} for identity in identities]

        def judge(prompt):
            calls["judge_invocations"] += 1
            return json.dumps({"grade": "PARTIAL", "queries": ["detail", " DETAIL ", "question"],
                               "core_ids": ["core"]}), {}

        search_recipes.search = repeated_retrieve
        try:
            result = search_recipes.multi_round(root, "question", limit=2, complete=judge)
        finally:
            search_recipes.search = original
        calls["returned_ids"] = [row["id"] for row in result["results"]]
        measured["query_exhaustion"] = calls

        root = os.path.join(directory, "withdrawal")
        fact_store.migrate(root)
        fact, _ = hierarchical.add_fact(root, "parser validates incoming grammar")

        def withdraw(prompt):
            hierarchical.forget_fact(root, fact.id)
            return json.dumps({"grade": "COMPLETE", "queries": [], "core_ids": [fact.id]}), {}

        result = search_recipes.multi_round(root, "parser", complete=withdraw, embedder="none", reranker="none")
        measured["withdrawal"] = {"withdrawn_fact_returned": fact.id in [row["id"] for row in result["results"]]}

        root = os.path.join(directory, "lexical-race")
        fact_store.migrate(root)
        anchor, _ = hierarchical.add_fact(root, "parser validates incoming grammar", valid_from="2024-01-01T00:00:00Z")
        support, _ = hierarchical.add_fact(root, "Use a bounded syntax tree before execution")

        def invalidate(query, limit):
            hierarchical.invalidate_fact(root, anchor.id)
            return []

        rows = search_recipes.search(root, "parser", vector_search=invalidate, embedder="none", reranker="none",
            backend=Backend({"memory:" + anchor.id: ["memory:" + support.id]}))
        measured["lexical_race"] = {"returned_count_after_seed_invalidation": len(rows)}
    after = fingerprint(source)
    if before != after:
        raise RuntimeError("source changed during measurement")
    for name, module in tuple(sys.modules.items()):
        if name == "commontrace" or name.startswith("commontrace."):
            filename = os.path.abspath(module.__file__)
            if os.path.commonpath([source, filename]) != source:
                raise RuntimeError("runtime import escaped the requested source: " + name)
            modules[name] = filename
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source,
                            capture_output=True, text=True, check=True).stdout.strip()
    return {"source": source, "commit": commit, "source_sha256": before, "source_unchanged": True,
            "module_paths": modules, "measurements": measured}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", required=True)
    parser.add_argument("--output")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    sources = [os.path.realpath(source) for source in args.source]
    if args.worker:
        if len(sources) != 1:
            parser.error("worker requires exactly one source")
        print(json.dumps(worker(sources[0]), sort_keys=True))
        return 0
    runs = []
    for source in sources:
        process = subprocess.run([sys.executable, "-I", os.path.abspath(__file__), "--worker", "--source", source],
                                 capture_output=True, text=True, check=True)
        runs.append(json.loads(process.stdout))
    with open(__file__, "rb") as stream:
        harness_hash = hashlib.sha256(stream.read()).hexdigest()
    report = {"scope": "deterministic synthetic policy fixtures; real SQLite; no model or latency measurement",
              "argv": sys.argv if argv is None else argv, "harness_sha256": harness_hash, "runs": runs}
    if args.output:
        with open(args.output, "w", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, sort_keys=True)
            stream.write("\n")
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
