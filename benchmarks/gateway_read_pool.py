"""Actual HTTP recall: fresh-store lifecycle versus bounded read reuse.

The corpus is a deterministic operational-note workload, not customer traffic.
No LLM, embeddings or oracle answer enters either request path. Both paths use
the same production recall and compare returned contexts before timing claims.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import statistics
import tempfile
import threading
import time
from contextlib import contextmanager

from unimem.conversation import Store
from unimem.conversation.pool import READ_POOL
from unimem.gateway import Gateway, make_http_server


class FreshStores:
    def discard_root(self, root):
        pass

    @contextmanager
    def lease(self, root, space, *, query=""):
        with Store(root, space, create=False) as store:
            yield store


def measure(root, queries, fresh):
    gateway = Gateway(root, token="local-evaluation-token-not-a-credential")
    if fresh:
        gateway._conversation_pool = FreshStores()
    server = make_http_server(gateway, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=30)
    samples, bodies = [], []
    try:
        for query in queries:
            started = time.perf_counter()
            connection.request("POST", "/v1/conversation/recall", json.dumps({
                "space": "operations", "question": query, "budget": 1500}), {
                    "Authorization": "Bearer local-evaluation-token-not-a-credential",
                    "Content-Type": "application/json"})
            response = connection.getresponse()
            result = json.loads(response.read())
            samples.append(1000 * (time.perf_counter() - started))
            if response.status != 200:
                raise RuntimeError(f"HTTP evaluation failed: {response.status}")
            bodies.append({"context": result["context"], "turns": result["turns"], "tokens": result["tokens"]})
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    warm = sorted(samples[1:])
    return {"cold_ms": samples[0], "warm_p50_ms": statistics.median(warm),
            "warm_p95_ms": warm[min(len(warm) - 1, int(len(warm) * .95))],
            "response_sha256": hashlib.sha256(json.dumps(bodies, sort_keys=True).encode()).hexdigest()}


def run(turns=2000, requests=40):
    old_setting = os.environ.get("UNIMEM_CONVERSATION_EMBEDDER")
    os.environ["UNIMEM_CONVERSATION_EMBEDDER"] = "none"
    try:
        with tempfile.TemporaryDirectory(prefix="unimem-http-recall-") as root:
            with Store(root, "operations") as store:
                for start in range(0, turns, 50):
                    store.add(f"shift-{start // 50}", [{"role": "user", "text":
                        f"Service component{i % 20} deployment runbook: verify health checks and snapshot backup. "
                        f"Incident note {i} records rollout approval and recovery owner."}
                        for i in range(start, min(start + 50, turns))], session_at="2024-01-01")
            results = {}
            for workload, queries in {
                "repeated": ["component7 deployment health checks"] * requests,
                "distinct": [f"component{i % 20} deployment recovery note {i}" for i in range(requests)],
            }.items():
                READ_POOL.discard_root(root)
                fresh = measure(root, queries, True)
                pooled = measure(root, queries, False)
                results[workload] = {"fresh": fresh, "pooled": pooled,
                    "equivalent": fresh["response_sha256"] == pooled["response_sha256"],
                    "warm_p50_speedup": fresh["warm_p50_ms"] / pooled["warm_p50_ms"]}
            return {"transport": "HTTP/1.1 loopback, bearer auth, production gateway and recall",
                    "turns": turns, "requests_per_arm": requests, "workloads": results,
                    "scope": "simulated operational notes; excludes model inference and remote network latency"}
    finally:
        if old_setting is None:
            os.environ.pop("UNIMEM_CONVERSATION_EMBEDDER", None)
        else:
            os.environ["UNIMEM_CONVERSATION_EMBEDDER"] = old_setting


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turns", type=int, default=2000)
    parser.add_argument("--requests", type=int, default=40)
    args = parser.parse_args(argv)
    if not 1 <= args.turns <= 100_000 or not 2 <= args.requests <= 1000:
        parser.error("turns must be 1..100000 and requests 2..1000")
    report = run(args.turns, args.requests)
    print(json.dumps(report, sort_keys=True))
    return int(not all(row["equivalent"] for row in report["workloads"].values()))


if __name__ == "__main__":
    raise SystemExit(main())
