"""Profile localized belief maintenance against an independent checkout."""
import argparse
import json
import os
import shutil
import statistics
import sys
import tempfile
import time


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("checkout")
    p.add_argument("--owners", type=int, default=10000)
    p.add_argument("--runs", type=int, default=5)
    a = p.parse_args()
    if a.runs <= 0 or a.owners <= 0:
        p.error("counts must be positive and pending turns non-negative")
    sys.path.insert(0, a.checkout)
    from commontrace.conversation.store import Store

    with tempfile.TemporaryDirectory() as root:
        with Store(root, "memory") as s:
            start = time.perf_counter()
            for name, date, place in [("past", "2023-01-01", "London"), ("current", "2025-01-01", "Paris")]:
                s.add(
                    name,
                    (
                        {"speaker": f"Person{i}", "text": f"I live in {place}.", "id": f"{name}-{i}"}
                        for i in range(a.owners)
                    ),
                    session_at=date,
                )
            s.add("remove", [{"speaker": "Target", "text": "I live in Rome."}], session_at="2024-01-01")
            s.add("keep", [{"speaker": "Target", "text": "I live in Oslo."}], session_at="2023-01-01")
            s.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            prep = time.perf_counter() - start
        timings = []
        changes = []
        valid = []
        for i in range(a.runs):
            trial = os.path.join(root, f"trial{i}")
            os.makedirs(os.path.join(trial, "memory", "conversations"))
            shutil.copyfile(
                os.path.join(root, "memory", "conversations", "memory.db"),
                os.path.join(trial, "memory", "conversations", "memory.db"),
            )
            with Store(trial, "memory") as s:
                before = s.db.total_changes
                start = time.perf_counter()
                s.delete_session("remove")
                timings.append((time.perf_counter() - start) * 1000)
                changes.append(s.db.total_changes - before)
                valid.append(
                    s.facts(candidates=[s.db.execute("SELECT id FROM facts WHERE owner='target'").fetchone()[0]])[0][
                        "statement"
                    ]
                    == "I live in Oslo."
                )
        print(
            json.dumps(
                {
                    "owners": a.owners,
                    "facts": a.owners * 2 + 2,
                    "runs_ms": timings,
                    "median_ms": statistics.median(timings),
                    "changed_rows": changes,
                    "history_correct": valid,
                    "preparation_seconds": prep,
                }
            )
        )


if __name__ == "__main__":
    main()
