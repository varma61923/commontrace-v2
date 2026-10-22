"""Official benchmark conversations through a deployed gateway process, over HTTP.

    python benchmarks/deployed_recall.py --dataset locomo --data locomo10.json --limit 200 \
        --budget 6000 --adaptive-budget --max-budget 16000 --concurrency 4

A separate `unimem gateway` process serves a fresh store. Every session is written
through POST /v1/conversation/add and every question is asked through POST
/v1/conversation/recall, as a deployed agent would. The report gives ingest and
recall latency, recall throughput under concurrent clients, gold-evidence recall
per category, and parity: whether each served context is byte-identical to an
in-process `recall` over the same store with the same options.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import secrets
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmarks.conversation_bench import _load_cases  # noqa: E402
from benchmarks.measurement import gold_turns  # noqa: E402
from unimem.conversation import Options, Store, recall  # noqa: E402

ADD_BATCH = 200  # messages per add request, under the transport's per-request bound

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _percentiles(values: list[float]) -> dict:
    ordered = sorted(values)
    if not ordered:
        return {}
    return {"p50_ms": round(ordered[int(0.5 * (len(ordered) - 1))], 1),
            "p95_ms": round(ordered[int(0.95 * (len(ordered) - 1))], 1),
            "mean_ms": round(statistics.mean(ordered), 1)}


class Gateway:
    def __init__(self, root: str):
        self.port, self.token = _free_port(), secrets.token_urlsafe(24)
        self.process = subprocess.Popen(
            [sys.executable, "-m", "unimem.cli", "gateway", "--dest", root, "--host", "127.0.0.1",
             "--port", str(self.port), "--token", self.token],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("gateway exited: " + self.process.stderr.read().decode(errors="replace")[-500:])
            try:
                if self.call("GET", "/v1/health/live")[0] == 200:
                    return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("gateway did not become ready")

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=300)
        try:
            connection.request(method, path, json.dumps(body) if body is not None else None,
                               {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read() or b"{}")
        finally:
            connection.close()

    def close(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(10)
        except subprocess.TimeoutExpired:
            self.process.kill()


def run(args) -> dict:
    from unimem.conversation import embed

    cases = _load_cases(args)
    root = args.root or tempfile.mkdtemp(prefix="unimem-deployed-")
    # Vectors can only be prepared at write time when this host has an embedder.
    cache_vectors = bool(args.cache_vectors and embed.configured())
    gateway = Gateway(root)
    ingest_ms, add_turns = [], 0
    try:
        for space, sessions, _now, _questions in cases:
            space = re.sub(r"[^A-Za-z0-9._-]", "_", space)
            for session, date, messages in sessions:
                for start in range(0, len(messages), ADD_BATCH):
                    batch = [{key: str(m[key]) for key in ("id", "speaker", "role", "text") if m.get(key) is not None}
                             for m in messages[start:start + ADD_BATCH]]
                    started = time.perf_counter()
                    status, body = gateway.call("POST", "/v1/conversation/add", {
                        "space": space, "session": session, "session_at": date, "messages": batch,
                        "cache_vectors": cache_vectors})
                    if status != 200:
                        raise RuntimeError(f"add failed ({status}): {body}")
                    ingest_ms.append(1000 * (time.perf_counter() - started))
                    add_turns += len(batch)

        requests = []
        for space, sessions, now, questions in cases:
            space = re.sub(r"[^A-Za-z0-9._-]", "_", space)
            for q in questions:
                body = {"space": space, "question": q["question"], "now": now, "budget": args.budget}
                if args.adaptive_budget:
                    body.update(adaptive_budget=True, max_budget=args.max_budget)
                requests.append((space, sessions, now, q, body))

        def ask(item):
            started = time.perf_counter()
            status, served = gateway.call("POST", "/v1/conversation/recall", item[4])
            if status != 200:
                raise RuntimeError(f"recall failed ({status}): {served}")
            return item, served, 1000 * (time.perf_counter() - started)

        started = time.perf_counter()
        with ThreadPoolExecutor(max(1, args.concurrency)) as pool:
            answers = list(pool.map(ask, requests))
        wall = time.perf_counter() - started
    finally:
        gateway.close()

    opts = Options(budget=args.budget, adaptive_budget=args.adaptive_budget,
                   max_budget=max(args.budget, args.max_budget))
    by_type, latencies, identical = defaultdict(list), [], 0
    for (space, sessions, now, q, _body), served, ms in answers:
        latencies.append(ms)
        with Store(root, space, create=False) as store:
            local = recall(store, q["question"], now=now, options=opts)
            identical += local.context == served["context"]
            delivered = {turn.ref for turn in store.turns(served["turns"]).values()}
        gold = gold_turns(q["evidence"], sessions)
        by_type[q["type"]].append({"evidence": len(gold & delivered) / len(gold) if gold else None,
                                   "tokens": served["tokens"]})

    def block(rows):
        known = [r["evidence"] for r in rows if r["evidence"] is not None]
        return {"n": len(rows), "evidence": round(statistics.mean(known), 4) if known else None,
                "tokens": round(statistics.mean(r["tokens"] for r in rows), 1)}

    return {
        "dataset": args.dataset, "questions": len(answers), "conversations": len(cases),
        "budget": args.budget, "adaptive_budget": args.adaptive_budget, "max_budget": args.max_budget,
        "concurrency": args.concurrency, "vectors_prepared_at_write": cache_vectors,
        "ingest": {"turns": add_turns, "requests": len(ingest_ms), **_percentiles(ingest_ms),
                   "turns_per_second": round(add_turns / (sum(ingest_ms) / 1000), 1) if ingest_ms else None},
        "recall": {**_percentiles(latencies), "queries_per_second": round(len(answers) / wall, 2)},
        "parity": {"identical_contexts": identical, "of": len(answers)},
        "overall": block([r for rows in by_type.values() for r in rows]),
        "by_type": {t: block(rows) for t, rows in sorted(by_type.items())},
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dataset", choices=("locomo", "longmemeval", "beam"), required=True)
    p.add_argument("--data", required=True)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--root", default=None, help="store root for the gateway (default: a fresh temporary one)")
    p.add_argument("--budget", type=int, default=1500)
    p.add_argument("--adaptive-budget", action="store_true")
    p.add_argument("--max-budget", type=int, default=12_000)
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--lazy-vectors", dest="cache_vectors", action="store_false",
                   help="embed on first recall instead of at write time (cold first queries)")
    p.add_argument("--out")
    args = p.parse_args(argv)
    args.question_exclusions = None
    args.personas = ""
    result = run(args)
    print(json.dumps(result, indent=2))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as sink:
            json.dump(result, sink, indent=2)
    return 0 if result["parity"]["identical_contexts"] == result["parity"]["of"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
