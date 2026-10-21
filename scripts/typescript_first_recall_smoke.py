#!/usr/bin/env python3
"""Pack/install the local TypeScript SDK and verify add + reflect via a local gateway.

All npm caches, package builds, the gateway store, and the consumer install live in
a TemporaryDirectory. The SDK tarball is local; this does not test a registry
publication or a published ``npm install @unimem/...`` flow.
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SDK = ROOT / "sdk" / "typescript"
FACT = "For the purple platypus task, the local smoke uses port 4832 and the answer is violet."
QUERY = "For the purple platypus task, what port does the local smoke use?"
TOKEN = "typescript-first-recall-smoke-local-token"
GATEWAY_LINE = re.compile(r"API:\s+http://localhost:(\d+)/v1/openapi\.json")


def run_step(
    label: str, command: list[str], *, cwd: Path, env: dict[str, str]
) -> tuple[subprocess.CompletedProcess[str], float]:
    started = time.perf_counter()
    result = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, check=False)
    elapsed = time.perf_counter() - started
    if result.returncode:
        raise RuntimeError(
            f"{label} failed ({result.returncode}):\n{result.stdout}\n{result.stderr}"
        )
    return result, elapsed


def start_gateway(store: Path, env: dict[str, str]) -> tuple[subprocess.Popen[str], str, float]:
    started = time.perf_counter()
    process = subprocess.Popen(
        [sys.executable, "-u", "-m", "unimem", "gateway", "--dest", str(store),
         "--token", TOKEN, "--host", "127.0.0.1", "--port", "0"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert process.stderr is not None
    lines: queue.Queue[str] = queue.Queue()

    def read_stderr() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            lines.put(line)

    threading.Thread(target=read_stderr, daemon=True).start()
    seen: list[str] = []
    deadline = time.monotonic() + 20
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("local gateway exited during startup:\n" + "".join(seen))
            try:
                line = lines.get(timeout=0.1)
            except queue.Empty:
                continue
            seen.append(line)
            match = GATEWAY_LINE.search(line)
            if match:
                url = f"http://localhost:{match.group(1)}"
                # The startup line follows bind(); a health request confirms the route responds.
                with urllib.request.urlopen(url + "/v1/health/live", timeout=2) as response:
                    if response.status != 200:
                        raise RuntimeError(f"gateway liveness returned HTTP {response.status}")
                return process, url, time.perf_counter() - started
        raise RuntimeError("timed out waiting for local gateway startup:\n" + "".join(seen))
    except BaseException:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        raise


def main() -> int:
    total_started = time.perf_counter()
    durations: dict[str, float] = {}
    env = os.environ.copy()
    env["UNIMEM_OFFLINE"] = "1"

    with tempfile.TemporaryDirectory(prefix="unimem-ts-first-recall-") as temporary:
        scratch = Path(temporary)
        sdk_cache = scratch / "sdk-npm-cache"
        consumer_cache = scratch / "consumer-npm-cache"
        build = scratch / "sdk"
        consumer = scratch / "consumer"
        store = scratch / "store"

        shutil.copytree(SDK, build, ignore=shutil.ignore_patterns("node_modules", "dist"))
        _, durations["sdk_dependencies_install"] = run_step(
            "SDK dependency install",
            ["npm", "ci", "--no-audit", "--no-fund", "--cache", str(sdk_cache)],
            cwd=build, env=env,
        )
        _, durations["sdk_tests"] = run_step("SDK build and tests", ["npm", "test"], cwd=build, env=env)
        packed, durations["pack"] = run_step(
            "SDK pack", ["npm", "pack", "--json", "--pack-destination", str(scratch)],
            cwd=build, env=env,
        )
        pack_info = json.loads(packed.stdout)
        tarball = scratch / pack_info[0]["filename"]
        if not tarball.is_file():
            raise RuntimeError(f"npm pack did not create {tarball.name}")

        consumer.mkdir()
        (consumer / "package.json").write_text(
            json.dumps({"name": "unimem-local-first-recall", "private": True, "type": "module"}) + "\n",
            encoding="utf-8",
        )

        first_value_started = time.perf_counter()
        _, durations["consumer_npm_install"] = run_step(
            "consumer npm install",
            ["npm", "install", "--no-audit", "--no-fund", "--cache", str(consumer_cache), str(tarball)],
            cwd=consumer, env=env,
        )

        init_started = time.perf_counter()
        run_step("temporary store init", [sys.executable, "-m", "unimem", "init", "--dest", str(store)],
                 cwd=ROOT, env=env)
        run_step("disable holdout in disposable smoke store",
                 [sys.executable, "-m", "unimem", "experiment", "--dest", str(store),
                  "--configure", "--rate", "0", "--note", "deterministic local first-recall smoke"],
                 cwd=ROOT, env=env)
        durations["store_setup"] = time.perf_counter() - init_started

        gateway: subprocess.Popen[str] | None = None
        try:
            gateway, url, durations["gateway_start"] = start_gateway(store, env)
            smoke = consumer / "smoke.mjs"
            smoke.write_text(
                "import assert from 'node:assert/strict';\n"
                "import {MemoryClient} from '@unimem/hub-client';\n"
                "const [url, token] = process.argv.slice(2);\n"
                f"const fact = {json.dumps(FACT)};\n"
                f"const query = {json.dumps(QUERY)};\n"
                "const memory = new MemoryClient({url, token, context: ['user:npm-smoke']});\n"
                "const added = await memory.add(fact);\n"
                "assert.ok(added.facts.some(row => row.statement === fact), 'add must return the stored fact');\n"
                "const recalled = await memory.reflect(query, {budget: 600});\n"
                "assert.ok(recalled.evidence.some(row => row.text === fact), 'reflect must return the added fact');\n"
                "console.log(JSON.stringify({addedFacts: added.facts.length, recalled: recalled.evidence.length, "
                "fact: recalled.evidence.find(row => row.text === fact).text}));\n",
                encoding="utf-8",
            )
            _, durations["add_and_reflect"] = run_step(
                "packed TypeScript SDK add + reflect",
                ["node", str(smoke), url, TOKEN], cwd=consumer, env=env,
            )
            durations["npm_install_to_recall"] = time.perf_counter() - first_value_started
            result = json.loads(_.stdout)
        finally:
            if gateway is not None:
                gateway.terminate()
                try:
                    gateway.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    gateway.kill()
                    gateway.wait(timeout=5)

    durations["script_total"] = time.perf_counter() - total_started
    print(json.dumps({
        "status": "passed",
        "package": pack_info[0]["name"],
        "package_version": pack_info[0]["version"],
        "node": subprocess.run(["node", "--version"], capture_output=True, text=True, check=True).stdout.strip(),
        "npm": subprocess.run(["npm", "--version"], capture_output=True, text=True, check=True).stdout.strip(),
        "consumer_install_to_verified_reflect_seconds": round(durations["npm_install_to_recall"], 3),
        "durations_seconds": {key: round(value, 3) for key, value in durations.items()},
        "recall": result,
        "limits": [
            "installed a local npm pack tarball, not a registry-published package",
            "gateway ran locally on loopback with a disposable store and temporary token",
            "holdout was disabled only in the disposable smoke store for deterministic first recall",
            "UNIMEM_OFFLINE=1; add used explicit local mode and reflect used local retrieval",
        ],
    }, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"typescript first-recall smoke failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
