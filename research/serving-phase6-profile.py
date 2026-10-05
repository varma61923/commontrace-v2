"""Measure actual bounded event-report handlers against a selected checkout."""

import argparse
import datetime
import json
import os
import statistics
import sys
import tempfile
import time

p = argparse.ArgumentParser()
p.add_argument("checkout")
p.add_argument("--events", type=int, default=5000)
p.add_argument("--pages", type=int, default=100)
p.add_argument("--runs", type=int, default=5)
a = p.parse_args()
if a.events < 1 or not 1 <= a.pages <= 500 or a.runs < 1:
    p.error("events/runs must be positive; pages must be between 1 and 500")
sys.path.insert(0, os.path.abspath(a.checkout))
from commontrace import gateway  # noqa: E402 -- select measured source before importing

with tempfile.TemporaryDirectory() as root:
    os.makedirs(os.path.join(root, "memory"))
    now = datetime.datetime(2026, 10, 5, tzinfo=datetime.timezone.utc)
    path = os.path.join(root, "memory", gateway.EVENTS_NAME)
    with open(path, "w", encoding="utf-8") as fh:
        for n in range(a.events):
            event = {
                "kind": "outcome" if n % 2 else "recall",
                "occasion_id": f"occasion-{n}",
                "agent_id": f"agent-{n % 40}",
                "at": (now + datetime.timedelta(seconds=n)).isoformat(),
                "succeeded": n % 3 > 0,
                "withheld": n % 4,
                "protected": n % 2,
                "quarantined": 0,
            }
            fh.write(json.dumps(event, separators=(",", ":")) + "\n")
    paths = ["/v1/status", "/v1/agents"] + [f"/v1/occasions?limit={n}" for n in range(1, a.pages + 1)]
    durations = []
    decoded = []
    sizes = []
    first_data = None
    loader = gateway.json.loads
    for run in range(a.runs):
        instance = gateway.Gateway(root, token="x" * 40)
        count = [0]

        def loads(value, *args, **kwargs):
            count[0] += 1
            return loader(value, *args, **kwargs)

        gateway.json.loads = loads
        started = time.perf_counter()
        responses = [instance.handle("GET", path, trusted=True) for path in paths]
        durations.append(round((time.perf_counter() - started) * 1000, 3))
        decoded.append(count[0])
        gateway.json.loads = loader
        sizes.append(len(instance._memo))
        data = [loader(response.body) for response in responses]
        for item in data:
            for agent in item.get("agents", []):
                agent.pop("seconds_since_seen", None)  # wall time changes across separate processes
        if first_data is None:
            first_data = data
        assert first_data == data
        assert all(response.status == 200 for response in responses)
    print(
        json.dumps(
            {
                "events": a.events,
                "pages": a.pages,
                "requests": len(paths),
                "elapsed_ms": durations,
                "median_ms": statistics.median(durations),
                "decoded_json_rows": decoded,
                "memo_entries": sizes,
                "responses": first_data,
            },
            indent=2,
        )
    )
