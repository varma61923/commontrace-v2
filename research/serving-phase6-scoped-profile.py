"""Actual repeated scoped lesson recall; fixture creation is outside request timing."""

import argparse
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
p.add_argument("--runs", type=int, default=5)
a = p.parse_args()
if min(a.lessons, a.requests, a.runs) < 1:
    p.error("all workload counts must be positive")
sys.path.insert(0, os.path.abspath(a.checkout))
from commontrace import frontmatter, gateway, retrieval  # noqa: E402 -- select measured checkout

with tempfile.TemporaryDirectory() as root:
    folder = os.path.join(root, "memory", "lessons")
    os.makedirs(folder)
    for n in range(a.lessons):
        fm = {
            "name": f"lesson_{n:05}",
            "status": "active",
            "importance": 3,
            "scopes": ["container:alpha" if n % 2 else "container:bravo"],
            "description": f"Payment retry guidance for service {n}",
            "applies_when": "A payment retry request times out",
            "tags": ["payments", "retry"],
        }
        frontmatter.write(
            os.path.join(folder, f"lesson_{n:05}.md"), fm, "## Rule\nUse an idempotency key when retrying a payment.\n"
        )
    headers = {"Host": "localhost", "Authorization": "Bearer " + "x" * 40, "X-Container-Tag": "alpha"}
    values, builds = [], []
    first = None
    for run in range(a.runs):
        instance = gateway.Gateway(root, token="x" * 40)
        warm = instance.handle(
            "POST",
            "/v1/recall",
            headers,
            json.dumps({"occasion_id": "warm", "query": "payment retry guidance"}).encode(),
        )
        assert warm.status == 200
        real = retrieval._build_index
        count = [0]

        def build(*args, **kwargs):
            count[0] += 1
            return real(*args, **kwargs)

        retrieval._build_index = build
        started = time.perf_counter()
        responses = [
            instance.handle(
                "POST",
                "/v1/recall",
                headers,
                json.dumps({"occasion_id": f"occasion-{n}", "query": "payment retry guidance"}).encode(),
            )
            for n in range(a.requests)
        ]
        values.append(round((time.perf_counter() - started) * 1000, 3))
        builds.append(count[0])
        retrieval._build_index = real
        parsed = [json.loads(response.body) for response in responses]
        assert all(response.status == 200 for response in responses)
        if first is None:
            first = parsed
        assert parsed == first
    print(
        json.dumps(
            {
                "lessons": a.lessons,
                "requests_per_run": a.requests,
                "elapsed_ms": values,
                "median_ms": statistics.median(values),
                "lexical_index_builds": builds,
                "responses": first,
            },
            indent=2,
        )
    )
