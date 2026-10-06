"""Profile real scoped recall and direct local ranking without model calls."""
import argparse
import hashlib
import json
import os
import statistics
import sys
import tempfile
import time

p = argparse.ArgumentParser()
p.add_argument("checkout")
p.add_argument("--lessons", type=int, default=1000)
p.add_argument("--requests", type=int, default=100)
p.add_argument("--distinct", action="store_true", help="cycle distinct service-number queries")
a = p.parse_args()
sys.path.insert(0, os.path.abspath(a.checkout))
from commontrace import frontmatter, gateway, lesson_cache, retrieval  # noqa: E402


def summary(samples):
    ordered = sorted(samples)
    return {"samples_ms": samples, "p50_ms": statistics.median(samples),
            "p95_ms": ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)]}


with tempfile.TemporaryDirectory() as root:
    folder = os.path.join(root, "memory", "lessons")
    os.makedirs(folder)
    for n in range(a.lessons):
        frontmatter.write(os.path.join(folder, f"lesson_{n:05}.md"), {
            "name": f"lesson_{n:05}", "status": "active", "importance": 3,
            "scopes": ["container:alpha" if n % 2 else "container:bravo"],
            "description": f"Payment retry guidance for service {n}",
            "applies_when": "A payment retry request times out", "tags": ["payments", "retry"],
        }, "## Rule\nUse an idempotency key when retrying a payment.\n")
    headers = {"Host": "localhost", "Authorization": "Bearer " + "x" * 40, "X-Container-Tag": "alpha"}
    instance = gateway.Gateway(root, token="x" * 40)
    query = "payment retry guidance"
    expected = None

    def recall(query):
        response = instance.handle("POST", "/v1/recall", headers,
                                   json.dumps({"occasion_id": "fixed", "query": query}).encode())
        assert response.status == 200
        return json.loads(response.body)

    started = time.perf_counter()
    expected = recall(query)
    cold = (time.perf_counter() - started) * 1000
    samples = []
    response_values = []
    for n in range(a.requests):
        current_query = query + f" service {n}" if a.distinct else query
        started = time.perf_counter()
        value = recall(current_query)
        samples.append(round((time.perf_counter() - started) * 1000, 6))
        response_values.append(value)
        if not a.distinct:
            assert value == expected
    scoped = summary(samples)
    scoped["cold_ms"] = cold
    rows, terms = lesson_cache.load_active_with_terms(root)
    expected_rank = retrieval.rank_lessons(query, rows, top_k=10, term_cache=terms)
    samples = []
    for n in range(a.requests):
        current_query = query + f" service {n}" if a.distinct else query
        started = time.perf_counter()
        rows, terms = lesson_cache.load_active_with_terms(root)
        value = retrieval.rank_lessons(current_query, rows, top_k=10, term_cache=terms)
        samples.append(round((time.perf_counter() - started) * 1000, 6))
        if not a.distinct:
            assert value == expected_rank
    local = summary(samples)
    scans = [0]
    real_scan = lesson_cache.os.scandir

    def scan(*args, **kwargs):
        scans[0] += 1
        return real_scan(*args, **kwargs)

    lesson_cache.os.scandir = scan
    samples = []
    for n in range(a.requests):
        started = time.perf_counter()
        lesson_cache.source_fingerprint(root)
        samples.append(round((time.perf_counter() - started) * 1000, 6))
    lesson_cache.os.scandir = real_scan
    listing = summary(samples)
    listing["directory_scans"] = scans[0]
    print(json.dumps({"lessons": a.lessons, "requests": a.requests, "distinct": a.distinct, "scoped_recall": scoped,
                      "local_retrieval": local, "listing": listing,
                      "response_sha256": hashlib.sha256(
                          json.dumps(response_values, sort_keys=True).encode()).hexdigest()}, indent=2))
