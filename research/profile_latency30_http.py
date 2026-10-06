"""Measure real persistent HTTP requests, including socket and JSON work.

Uses local synthetic lessons, no model calls. Run against separate checkouts;
compare response hashes as well as cold and warm latency distributions.
"""

import argparse
import hashlib
import http.client
import json
import math
import os
import platform
import statistics
import sys
import tempfile
import threading
import time


def distribution(samples):
    ordered = sorted(samples)
    return {
        "samples_ms": samples,
        "p50_ms": statistics.median(samples),
        "p95_ms": ordered[math.ceil(len(ordered) * 0.95) - 1],
        "p99_ms": ordered[math.ceil(len(ordered) * 0.99) - 1],
        "max_ms": ordered[-1],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout")
    parser.add_argument("--lessons", type=int, default=1000)
    parser.add_argument("--requests", type=int, default=50)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    if min(args.lessons, args.requests, args.runs) < 1:
        parser.error("all workload counts must be positive")
    sys.path.insert(0, os.path.abspath(args.checkout))
    from commontrace.gateway import Gateway, make_http_server
    from commontrace.reference.measure_local_latency import _VOCAB, build_store

    measurements = {name: {"cold": [], "warm": []} for name in ("health", "recall")}
    response_hashes = []
    for run in range(args.runs):
        with tempfile.TemporaryDirectory() as root:
            build_store(root, args.lessons)
            server = make_http_server(Gateway(root, token="x" * 40), "127.0.0.1", 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            conn = http.client.HTTPConnection(*server.server_address, timeout=30)
            digest = hashlib.sha256()
            try:
                for name in measurements:
                    for n in range(args.requests + 1):
                        headers = {"Authorization": "Bearer " + "x" * 40}
                        if name == "recall":
                            query = " ".join(_VOCAB[(n + j) % len(_VOCAB)] for j in range(6))
                            body = json.dumps({"occasion_id": f"request-{n}", "query": query, "top_k": 3})
                            headers["Content-Type"] = "application/json"
                            method, path = "POST", "/v1/recall"
                        else:
                            body, method, path = None, "GET", "/v1/health"
                        start = time.perf_counter()
                        conn.request(method, path, body, headers)
                        response = conn.getresponse()
                        data = response.read()
                        decoded = json.loads(data)
                        elapsed = (time.perf_counter() - start) * 1000
                        if response.status != 200:
                            raise RuntimeError(f"{name}: HTTP {response.status}: {decoded}")
                        digest.update(json.dumps(decoded, sort_keys=True, separators=(",", ":")).encode())
                        measurements[name]["cold" if n == 0 else "warm"].append(elapsed)
            finally:
                conn.close()
                server.shutdown()
                server.server_close()
                thread.join()
            response_hashes.append(digest.hexdigest())
    if len(set(response_hashes)) != 1:
        raise RuntimeError("responses differ across identical local fixtures")
    result = {
        "checkout": os.path.abspath(args.checkout),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "lessons": args.lessons,
        "runs": args.runs,
        "warm_requests_per_endpoint_per_run": args.requests,
        "transport": "HTTP/1.1 persistent loopback; sequential requests; no TLS",
        "cold_scope": "new fixture and Gateway per run, shared process and warm OS cache",
        "model_calls": 0,
        "response_sha256": response_hashes[0],
        "endpoints": {
            name: {state: distribution(samples) for state, samples in values.items()}
            for name, values in measurements.items()
        },
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
