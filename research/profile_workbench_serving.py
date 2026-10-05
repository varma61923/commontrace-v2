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
p.add_argument("--runs", type=int, default=5)
p.add_argument("--reviews", type=int, default=0)
a = p.parse_args()
if a.lessons < 25 or a.runs < 1 or a.reviews < 0:
    p.error("lessons must be at least 25; runs positive and reviews nonnegative")
sys.path.insert(0, os.path.abspath(a.checkout))
from commontrace import frontmatter, gateway  # noqa: E402 -- select the measured checkout first

with tempfile.TemporaryDirectory() as root:
    folder = os.path.join(root, "memory", "lessons")
    os.makedirs(folder)
    fm = {
        "status": "active",
        "domain": "payments",
        "description": "Retry requests safely with idempotency keys.",
        "tags": ["payments", "retry", "http"],
        "applies_when": "A request times out.",
        "do_not_apply_when": "The request is read-only.",
        "importance": 3,
        "source_traces": ["one", "two"],
        "scopes": ["support"],
        "llm_draft": {"provider": "local", "usage": {"input_tokens": 40}},
    }
    body = "## Rule\nUse an idempotency key for every retried write request.\n\n## Why\nDuplicate charges.\n"
    for n in range(a.lessons):
        frontmatter.write(os.path.join(folder, f"lesson_{n:05}.md"), dict(fm, name=f"lesson_{n:05}"), body)
    for n in range(a.reviews):
        name = f"lesson_review_{n:05}"
        frontmatter.write(os.path.join(folder, name + ".md"), dict(fm, name=name, status="review"), body)
    target = "/v1/lessons?status=review&limit=25" if a.reviews else "/v1/lessons?status=active&limit=25&offset=25"
    expected = a.reviews or a.lessons
    read = frontmatter.read
    reads = [0]

    def count_read(path):
        reads[0] += 1
        return read(path)

    frontmatter.read = count_read
    g = gateway.Gateway(root, token="x" * 40)
    samples = []
    counts = []
    content = None
    for i in range(a.runs + 1):
        before = reads[0]
        start = time.perf_counter()
        response = g.handle("GET", target, trusted=True)
        samples.append(round((time.perf_counter() - start) * 1000, 3))
        counts.append(reads[0] - before)
        assert response.status == 200
        result = json.loads(response.body)
        assert result["total"] == expected and len(result["lessons"]) == min(25, expected)
        if content is None:
            content = result
        assert content == result
    print(
        json.dumps(
            {
                "checkout": a.checkout,
                "active_lessons": a.lessons,
                "review_lessons": a.reviews,
                "cold_ms": samples[0],
                "warm_ms": samples[1:],
                "median_warm_ms": statistics.median(samples[1:]),
                "source_reads_per_request": counts,
                "result": content,
            },
            indent=2,
        )
    )
