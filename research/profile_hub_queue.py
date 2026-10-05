"""Profile the real Postgres KB review queue in an isolated temporary schema.

Uses HUB_TEST_DATABASE_URL; never prints credentials or touches existing tables.
The selected baseline's queue functions are loaded from git, not reconstructed.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import json
import os
import platform
import statistics
import subprocess
import time
import tracemalloc
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from hub import crud
from hub.models import Base, Organization, Trace, Vote


def baseline_functions(ref: str):
    source = subprocess.run(
        ["git", "show", f"{ref}:hub/crud.py"], check=True, capture_output=True, text=True,
        cwd=Path(__file__).resolve().parents[1],
    ).stdout
    names = (
        "count_kb_review_queue", "kb_review_queue_and_total", "kb_review_queue",
        "_kb_review_queue_full",
    )
    parsed = ast.parse(source)
    module = ast.Module(
        body=[node for node in parsed.body if isinstance(node, ast.AsyncFunctionDef)
              and node.name in names], type_ignores=[],
    )
    if not any(node.name == "_kb_review_queue_full" for node in module.body):
        raise ValueError("baseline must contain the original full-materialization queue helper")
    namespace = dict(vars(crud))
    exec(compile(module, f"{ref}:hub/crud.py", "exec"), namespace)
    return {name: namespace[name] for name in names if not name.startswith("_")}


async def profile(args):
    url = os.environ["HUB_TEST_DATABASE_URL"]
    schema = "ct_queue_profile_" + uuid.uuid4().hex
    management = create_async_engine(url)
    async with management.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            version = (await connection.execute(text("SELECT version()"))).scalar_one()
        owner, voter = str(uuid.uuid4()), str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        context = "retrieval evidence chronology preference provenance " * 40
        solution = "preserve source ordering and validate the latest current state " * 25
        async with sessions.begin() as session:
            await session.execute(insert(Organization), [
                {"id": owner, "name": "operator"}, {"id": voter, "name": "voter"},
            ])
            for start in range(1, args.entries + 1, 200):
                batch = []
                for number in range(start, min(start + 200, args.entries + 1)):
                    batch.append(dict(
                        id=str(uuid.UUID(int=number)), org_id=owner, title=f"memory {number}",
                        context_text=context, solution_text=solution, tags=["memory"],
                        agent_type="code", shared_with_commons=True, commons_source="seed",
                        commons_signature=list(range(128)), commons_hits=number if number % 100 else 0,
                        commons_votes=3 if number % 50 == 0 else 0,
                        trust=0.2 if number % 50 == 0 else 0.8,
                        commons_review_after=now + timedelta(days=-1 if number % 20 == 0 else 90),
                    ))
                await session.execute(insert(Trace), batch)
            await session.execute(insert(Vote), [
                dict(trace_id=str(uuid.UUID(int=number)), org_id=voter,
                     vote_type="up", feedback_tag="security_concern")
                for number in range(1, args.entries + 1, 77)
            ])
        async with engine.begin() as connection:
            await connection.execute(text("ANALYZE traces"))
            await connection.execute(text("ANALYZE votes"))
        before = baseline_functions(args.baseline_ref)
        after = {name: getattr(crud, name) for name in before}
        results = {}
        for name in before:
            samples = {"before": [], "after": []}
            response = {}
            for iteration in range(args.runs + 1):
                for label, functions in (("before", before), ("after", after)):
                    async with sessions() as session:
                        started = time.perf_counter()
                        response[label] = await functions[name](session)
                        elapsed = (time.perf_counter() - started) * 1000
                    if iteration:
                        samples[label].append(round(elapsed, 3))
            assert response["before"] == response["after"], name
            results[name] = {label: {"milliseconds": values,
                                    "median_ms": round(statistics.median(values), 3)}
                             for label, values in samples.items()}
            results[name]["speedup"] = round(
                results[name]["before"]["median_ms"] / results[name]["after"]["median_ms"], 2
            )
        memory = {}
        for label, functions in (("before", before), ("after", after)):
            async with sessions() as session:
                tracemalloc.start()
                await functions["count_kb_review_queue"](session)
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
            memory[label] = peak
        print(json.dumps({
            "baseline_sha": args.baseline_ref, "python": platform.python_version(),
            "postgres": version, "entries": args.entries, "runs": args.runs,
            "context_characters": len(context), "solution_characters": len(solution),
            "page_limit": 50, "responses_identical": True, "results": results,
            "count_peak_python_bytes": memory,
            "limitations": "Synthetic local Postgres serving profile, not answer-quality evaluation. "
                           "Warm connection pool; schema preparation is excluded.",
        }, indent=2))
    finally:
        await engine.dispose()
        async with management.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await management.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", default="8846feaccf94f6ad9dc37787c3c1f8fb63acdcca")
    parser.add_argument("--entries", type=int, default=10_000)
    parser.add_argument("--runs", type=int, default=5)
    arguments = parser.parse_args()
    if arguments.entries < 77 or arguments.runs < 1:
        parser.error("entries must be at least 77 and runs must be positive")
    asyncio.run(profile(arguments))
