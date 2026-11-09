"""The UniMem conformance suite: what it takes to call an implementation "UniMem-compatible"."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import random
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

from unimem import experiment, frontmatter, learning_contract, paths, raw_export, revision, validate, value

SUITE_VERSION = "2"
OPS = ("assign", "ledger", "digest", "revision", "allocate", "learning")
MAX_GATEWAY_RESPONSE_BYTES = 8 * 1024 * 1024
VECTORS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "conformance_vectors.json")
SPEC_VECTORS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "protocol", "conformance", "vectors.json"
)
NOT_TESTED = (
    "what is not tested: statistics (intervals, sequential validity), the integrity audit, retrieval "
    "ranking, and anything about how outcomes are obtained."
)


@dataclass
class Result:
    name: str
    ok: bool
    detail: str = ""


def ref_assign(lesson: str, occasion: str, salt: str, rate: float) -> bool:
    return experiment.is_held_out(lesson, occasion, rate, salt)


def ref_ledger(rows: list[dict]) -> list[str]:
    """The hash chain over `rows` (see value.ValueReport.ledger)."""
    out, previous = [], value._LEDGER_GENESIS
    for index, r in enumerate(rows):
        money = r["occasions_improved"] * r["rate"]
        row = value._FIELD_SEP.join(
            (str(index), r["slug"], r["verdict"], f"{r['occasions_improved']:.6f}", f"{r['rate']:.6f}", f"{money:.6f}")
        )
        previous = hashlib.sha256((previous + value._FIELD_SEP + row).encode("utf-8")).hexdigest()
        out.append(previous)
    return out


def ref_digest(rows: list[list[str]]) -> str:
    payload = raw_export._DIGEST_DOMAIN + "\x1e" + "\x1e".join(sorted("\x1f".join(r) for r in rows))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def ref_revision(fm: dict, body: str) -> str:
    return revision.revision_of(fm, body)


def ref_allocate(schedule: dict, lesson: str, occasion: str) -> dict:
    """PROTOCOL 13.5: a schedule's digest, the rate it sets for `lesson`, and the resulting arm."""
    from unimem import allocation

    rate = schedule["rates"].get(lesson, schedule["default_rate"])
    return {
        "digest": allocation.schedule_digest(schedule),
        "rate": rate,
        "held_out": experiment.is_held_out(lesson, occasion, rate, schedule["salt"]),
    }


def answer(request: dict) -> dict:
    """The reference implementation of the stdio protocol."""
    op = request.get("op")
    if op == "assign":
        return {"held_out": ref_assign(request["lesson"], request["occasion"], request["salt"], request["rate"])}
    if op == "ledger":
        return {"hashes": ref_ledger(request["rows"])}
    if op == "digest":
        return {"digest": ref_digest(request["rows"])}
    if op == "revision":
        return {"revision": ref_revision(request["frontmatter"], request["body"])}
    if op == "allocate":
        return ref_allocate(request["schedule"], request["lesson"], request["occasion"])
    if op == "learning":
        return ref_learning(request)
    return {"error": f"unknown op {op!r}"}


def ref_learning(request: dict) -> dict:
    """Pure wire semantics, without granting authority to caller-supplied sources.

    The fixture's sources/configuration stand for backend-resolved values. This
    reference is conformance tooling, never an admission or persistence API.
    """
    operation, payload = request["operation"], request["payload"]
    try:
        learning_contract.validate_request(operation, payload)
        if operation == "configure":
            result = learning_contract.validate_config(payload)
        elif operation == "assign":
            result = learning_contract.make_assignment(
                request["config"],
                payload["occasion_id"],
                request["sources"],
                task_context=payload.get("task_context", ""),
                policy=request.get("policy"),
            )
        elif operation == "outcome":
            result = learning_contract.normalize_outcome(request["config"], payload["occasion_id"], payload)
        else:
            raise learning_contract.LearningError("unsupported reference learning operation")
        return {"schema": learning_contract.SCHEMA, "value": result}
    except ValueError as exc:
        return {"error": getattr(exc, "code", "invalid_learning_request"), "status": getattr(exc, "status_code", 400)}


def build_learning_vectors() -> list[dict]:
    config = {
        **learning_contract.validate_config(
            {
                "mode": "adaptive",
                "utility": {
                    "terms": {"completion": {"weight": 1, "low": 0, "high": 100, "better": "higher"}},
                },
            }
        ),
        "experiment_id": "conformance-experiment",
        "revision": 1,
        "salt": "unimem-learning-v1",
    }
    sources: list[dict] = [
        {"id": "memory", "revision": 3, "text": "Check the current revision.", "protected": False},
        {"id": "required", "revision": 2, "text": "Respect the mandatory constraint.", "protected": True},
    ]
    cases: list[dict] = []
    for mode in ("off", "observe", "adaptive"):
        cases.append({"operation": "configure", "payload": {"mode": mode}})
        for context in ("", "robot/pick", "modèle/tâche"):
            cases.append(
                {
                    "operation": "assign",
                    "config": {**config, "mode": mode},
                    "sources": sources,
                    "payload": {
                        "occasion_id": "episode-1",
                        "memory_ids": ["memory", "required"],
                        "task_context": context,
                    },
                    "policy": {
                        learning_contract.policy_key(row["id"], row["revision"], context): {
                            "rate": 1.0,
                            "disabled": True,
                        }
                        for row in sources
                    },
                }
            )
            cases.append({**cases[-1], "policy": {}})
    for outcome in (
        {"succeeded": True},
        {"succeeded": False},
        {"reward": 0.0},
        {"reward": 0.75},
        {"reward": 1.0},
        {"metrics": {"completion": 25}},
        {"metrics": {"completion": 100}},
        {"reward": True},
        {"reward": 1.1},
        {"metrics": {"unknown": 50}},
        {"succeeded": True, "reward": 0.5},
    ):
        cases.append({"operation": "outcome", "config": config, "payload": {"occasion_id": "episode-1", **outcome}})
    for config_payload in (
        {"mode": "unknown"},
        {"mode": "observe", "holdout_rate": True},
        {"mode": "observe", "min_arm": 9},
    ):
        cases.append({"operation": "configure", "payload": config_payload})
    for assignment_payload in (
        {"occasion_id": "episode-1", "memory_ids": []},
        {"occasion_id": "episode-1", "memory_ids": ["memory", "memory"]},
        {"occasion_id": "episode-1", "memory_ids": ["memory"], "task_context": "x" * 65},
    ):
        cases.append({"operation": "assign", "config": config, "sources": sources, "payload": assignment_payload})
    return [{"request": {"op": "learning", **case}, "response": ref_learning(case)} for case in cases]


def build_vectors() -> dict:
    """Generate the reference vectors. Deterministic: the same code gives the same file."""
    rng = random.Random("unimem-conformance-v1")
    assign = []
    names = ["lesson-a", "refund-policy", "grasp/soft-cup", "ünïcode-leçon", "x" * 80, "a b", "a\x1fb"]
    for i in range(160):
        lesson = rng.choice(names)
        occasion = rng.choice(
            [f"ep-{rng.randint(0, 10**6)}", f"robot-{rng.randint(1, 9)}/ep-{i}", "ocčasion-ß", str(i)]
        )
        salt = rng.choice(["default", "salt-1", "exp-2026-q1", "ünï"])
        rate = rng.choice([0.0, 0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0])
        assign.append(
            {
                "lesson": lesson,
                "occasion": occasion,
                "salt": salt,
                "rate": rate,
                "held_out": ref_assign(lesson, occasion, salt, rate),
            }
        )
    ledgers = []
    for n in (0, 1, 3, 6):
        rows = [
            {
                "slug": f"memory-{k}",
                "verdict": rng.choice(["HELPS", "HURTS"]),
                "occasions_improved": round(rng.uniform(-40, 400), 3),
                "rate": round(rng.uniform(1, 80), 2),
            }
            for k in range(n)
        ]
        ledgers.append({"rows": rows, "hashes": ref_ledger(rows)})
    digests = []
    for n in (0, 1, 5, 40):
        digest_rows = [
            [
                f"m{rng.randint(0, 3)}",
                f"o{k}",
                rng.choice(["injected", "withheld"]),
                rng.choice(["true", "false", ""]),
                "2026-01-01T00:00:00+00:00",
                "",
                "r1",
                "0.5",
                "salt-1",
            ]
            for k in range(n)
        ]
        digests.append({"rows": digest_rows, "digest": ref_digest(digest_rows)})
    revisions = []
    for fm, body in (
        (
            {"applies_when": "when  a  refund  is asked", "do_not_apply_when": "never", "tags": ["b", "a"]},
            "## Rule\nLink the policy.\n",
        ),
        (
            {"applies_when": "when a refund is asked", "do_not_apply_when": "never", "tags": ["a", "b"]},
            "## Rule\r\nLink the policy.\r\n\r\n\r\n",
        ),
        ({"applies_when": "ünï", "do_not_apply_when": "", "tags": []}, ""),
    ):
        revisions.append({"frontmatter": fm, "body": body, "revision": ref_revision(fm, body)})
    allocations = []
    for n in range(40):
        rates = {f"memory-{k}": rng.choice([0.05, 0.25, 0.5, 0.95]) for k in range(rng.randint(0, 4))}
        schedule = {
            "version": n % 3,
            "salt": rng.choice(["exp-1", "ünï", "pinned"]),
            "effective_from": f"2026-03-0{1 + n % 9}T09:00:05+00:00",
            "default_rate": rng.choice([0.5, 0.3]),
            "rates": dict(sorted(rates.items())),
            "policy": {"explore": 0.5, "monitor": 0.05, "era_occasions": 250},
            "basis": {"reason": "enabled"} if n % 2 else {"assignments": n * 10},
            "previous": "" if n % 3 == 0 else hashlib.sha256(str(n).encode()).hexdigest(),
            "created_at": f"2026-03-0{1 + n % 9}T09:00:00+00:00",
            "enabled": True,
        }
        lesson = rng.choice([*rates, "unlisted"])
        occasion = f"ep-{rng.randint(0, 10**6)}"
        allocations.append(
            {"schedule": schedule, "lesson": lesson, "occasion": occasion, **ref_allocate(schedule, lesson, occasion)}
        )
    return {
        "suite": SUITE_VERSION,
        "protocol": "1.0.0",
        "assign": assign,
        "ledger": ledgers,
        "digest": digests,
        "revision": revisions,
        "allocate": allocations,
        "learning": build_learning_vectors(),
    }


def load_vectors(path: str | None = None) -> dict:
    with open(path or VECTORS_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def run_exec(command: str, vectors: dict, timeout: float = 30.0, only: tuple[str, ...] | None = None) -> list[Result]:
    """Run `command` as a stdio program against every vector. One process, one JSON object per line."""
    ops = only or OPS
    if set(ops) - set(OPS):
        return [Result("exec", False, "unknown conformance layer")]
    vectors = {k: (v if k in ops else []) for k, v in vectors.items() if isinstance(v, list)}
    for operation in OPS:
        vectors.setdefault(operation, [])
    missing = [
        Result(f"vectors:{operation}", False, "no vectors supplied for the requested layer")
        for operation in ops
        if not vectors[operation]
    ]
    requests, expected = [], []
    for v in vectors["assign"]:
        requests.append(
            {"op": "assign", "lesson": v["lesson"], "occasion": v["occasion"], "salt": v["salt"], "rate": v["rate"]}
        )
        expected.append(("assign", {"held_out": v["held_out"]}))
    for v in vectors["ledger"]:
        requests.append({"op": "ledger", "rows": v["rows"]})
        expected.append(("ledger", {"hashes": v["hashes"]}))
    for v in vectors["digest"]:
        requests.append({"op": "digest", "rows": v["rows"]})
        expected.append(("digest", {"digest": v["digest"]}))
    for v in vectors["revision"]:
        requests.append({"op": "revision", "frontmatter": v["frontmatter"], "body": v["body"]})
        expected.append(("revision", {"revision": v["revision"]}))
    for v in vectors["allocate"]:
        requests.append({"op": "allocate", "schedule": v["schedule"], "lesson": v["lesson"], "occasion": v["occasion"]})
        expected.append(("allocate", {"digest": v["digest"], "rate": v["rate"], "held_out": v["held_out"]}))
    for v in vectors["learning"]:
        requests.append(v["request"])
        expected.append(("learning", v["response"]))
    if not requests:
        return missing
    try:
        argv = shlex.split(command)
        if not argv:
            return [Result("exec", False, "no command given")]
        proc = subprocess.run(
            argv,
            input="".join(json.dumps(r) + "\n" for r in requests),  # nosec B603
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return [Result("exec", False, f"no answer within {timeout:g}s")]
    except OSError as exc:
        return [Result("exec", False, f"cannot run {command!r}: {exc}")]
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if proc.returncode:
        return [
            Result("exec", False, f"program exited with status {proc.returncode}; stderr: {proc.stderr.strip()[:200]}")
        ]
    if len(lines) != len(requests):
        return [
            Result(
                "exec",
                False,
                f"sent {len(requests)} requests, got {len(lines)} answers"
                + (f"; stderr: {proc.stderr.strip()[:200]}" if proc.stderr.strip() else ""),
            )
        ]
    failures: dict[str, list[str]] = {}
    totals: dict[str, int] = {}
    for (op, want), request, line in zip(expected, requests, lines):
        totals[op] = totals.get(op, 0) + 1
        try:
            got = json.loads(line)
        except ValueError:
            got = {"error": "not JSON"}
        if got != want:
            failures.setdefault(op, []).append(
                f"{json.dumps(request)[:120]} -> {line[:120]}, expected {json.dumps(want)[:80]}"
            )
    return missing + [
        Result(
            f"vectors:{op}",
            op not in failures,
            f"{totals[op] - len(failures.get(op, []))}/{totals[op]} agree"
            + (f"; first miss: {failures[op][0]}" if op in failures else ""),
        )
        for op in OPS
        if op in ops and op in totals
    ]


def check_vectors_against_reference(vectors: dict) -> list[Result]:
    """The committed file must equal what the reference implementation produces: the file is the contract."""
    fresh = build_vectors()
    return [
        Result(
            f"reference:{k}",
            vectors.get(k) == fresh[k],
            "the committed vectors match the reference"
            if vectors.get(k) == fresh[k]
            else "the committed vectors differ from the reference implementation",
        )
        for k in OPS
    ]


def check_store(root: str) -> list[Result]:
    out: list[Result] = []
    for kind, directory, schema_name in (
        ("trace", paths.traces_dir(root), "trace.schema.json"),
        ("lesson", paths.lessons_dir(root), "lesson.schema.json"),
    ):
        schema = validate.load_schema(schema_name)
        bad, n = [], 0
        if os.path.isdir(directory):
            for name in sorted(os.listdir(directory)):
                if not name.endswith(".md") or name in ("README.md", "lesson_template.md"):
                    continue
                n += 1
                try:
                    fm, _ = frontmatter.read(os.path.join(directory, name))
                except Exception as exc:  # noqa: BLE001 - any parse failure is a conformance failure
                    bad.append(f"{name}: unreadable ({exc})")
                    continue
                errors = validate.validate(fm, schema)
                if errors:
                    bad.append(f"{name}: {errors[0]}")
        out.append(Result(f"store:{kind}s validate", not bad, f"{n} checked" + (f"; {bad[0]}" if bad else "")))
    from unimem import holdout_io

    rows, corrupt = holdout_io.read_log(root)
    out.append(
        Result(
            "store:assignment log readable",
            corrupt == 0,
            f"{len(rows)} assignments" + (f"; {corrupt} unreadable line(s)" if corrupt else ""),
        )
    )
    wrong = [
        r
        for r in rows
        if r.salt is not None
        and r.rate is not None
        and (ref_assign(r.lesson, r.occasion_id, r.salt, r.rate) == bool(r.injected))
    ]
    out.append(
        Result(
            "store:assignments follow the randomization",
            not wrong,
            f"{len(rows)} recomputed" + (f"; first miss: {wrong[0].lesson} on {wrong[0].occasion_id}" if wrong else ""),
        )
    )
    return out


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Conformance checks must inspect the configured endpoint itself and must
        # never send its bearer token to a redirect destination.
        return None


def _call(base: str, method: str, path: str, token: str | None, body: dict | None = None) -> tuple[int, dict]:
    if urllib.parse.urlsplit(base).scheme not in ("http", "https"):
        return 0, {"error": "the gateway URL must be http or https"}
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(base.rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=10) as resp:  # nosec B310
            payload = resp.read(MAX_GATEWAY_RESPONSE_BYTES + 1)
            if len(payload) > MAX_GATEWAY_RESPONSE_BYTES:
                return 0, {"error": "gateway response exceeds 8 MiB"}
            try:
                result = json.loads(payload or b"{}")
            except ValueError:
                return 0, {"error": "gateway returned invalid JSON"}
            if not isinstance(result, dict):
                return 0, {"error": "gateway response must be a JSON object"}
            return resp.status, result
    except urllib.error.HTTPError as exc:
        with exc:
            payload = exc.read(MAX_GATEWAY_RESPONSE_BYTES + 1)
            if len(payload) > MAX_GATEWAY_RESPONSE_BYTES:
                return exc.code, {}
            try:
                result = json.loads(payload or b"{}")
            except ValueError:
                return exc.code, {}
            return exc.code, result if isinstance(result, dict) else {}
    except (urllib.error.URLError, OSError) as exc:
        return 0, {"error": str(exc)}


def _recall_partition(response: dict) -> tuple[set[str], set[str]] | None:
    deliver = response.get("deliver", [])
    withheld = response.get("withheld", [])
    if not isinstance(deliver, list) or not isinstance(withheld, list):
        return None
    ids: set[str] = set()
    withheld_ids: set[str] = set()
    for item in deliver:
        identity = item.get("id") if isinstance(item, dict) else item
        if not isinstance(identity, str):
            return None
        ids.add(identity)
    for item in withheld:
        if not isinstance(item, str):
            return None
        withheld_ids.add(item)
    return ids, withheld_ids


def check_gateway(base: str, token: str) -> list[Result]:
    run = hashlib.sha256(os.urandom(8)).hexdigest()[:10]
    out: list[Result] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        out.append(Result(name, bool(ok), detail))

    status, health = _call(base, "GET", "/v1/health", None)
    add("gateway:health needs no token", status == 200, f"HTTP {status}")
    if status != 200:
        return out
    status, _ = _call(base, "GET", "/v1/status", None)
    add("gateway:a call without a token is refused", status == 401, f"HTTP {status}")
    status, _ = _call(base, "GET", "/v1/status", "wrong-token")
    add("gateway:a wrong token is refused", status == 401, f"HTTP {status}")

    items = [{"id": f"{run}-m{i}", "text": f"memory {i}"} for i in range(6)]
    protected = {"id": f"{run}-safety", "text": "never move while a person is in reach", "protected": True}
    occasion = f"{run}-occ"
    status, first = _call(base, "POST", "/v1/recall", token, {"occasion_id": occasion, "items": [*items, protected]})
    add("gateway:recall answers", status == 200, f"HTTP {status}")
    if status != 200:
        return out
    partition = _recall_partition(first)
    if partition is None:
        add("gateway:recall partitions the candidates", False, "invalid recall response")
        return out
    delivered, withheld = partition
    candidates = {i["id"] for i in items}
    add(
        "gateway:recall partitions the candidates",
        delivered & withheld == set() and candidates <= (delivered | withheld),
        f"delivered {len(delivered)}, withheld {len(withheld)}",
    )
    add(
        "gateway:a protected memory is always delivered",
        protected["id"] in delivered and protected["id"] not in withheld,
    )
    status, second = _call(base, "POST", "/v1/recall", token, {"occasion_id": occasion, "items": [*items, protected]})
    again = _recall_partition(second) if status == 200 else None
    add("gateway:the same occasion gets the same partition", again == partition)

    status, _ = _call(base, "POST", "/v1/recall", token, {"items": "not a list"})
    add("gateway:a malformed request is a 400, not a crash", status == 400, f"HTTP {status}")

    status, rec = _call(base, "POST", "/v1/outcome", token, {"occasion_id": occasion, "succeeded": True})
    add("gateway:an outcome is recorded", status == 200 and rec.get("recorded") is True, f"HTTP {status}")
    status, rec = _call(base, "POST", "/v1/outcome", token, {"occasion_id": occasion, "succeeded": True})
    add("gateway:the same outcome again is harmless", status == 200 and rec.get("recorded") is False, f"HTTP {status}")
    status, _ = _call(base, "POST", "/v1/outcome", token, {"occasion_id": occasion, "succeeded": False})
    add("gateway:a conflicting outcome is refused", status == 409, f"HTTP {status}")
    status, _ = _call(base, "POST", "/v1/outcome", token, {"occasion_id": occasion + "-x", "succeeded": "yes"})
    add("gateway:an outcome must be a boolean", status == 400, f"HTTP {status}")
    return out


def check_learning_lifecycle(*, profiles: tuple[str, ...] = ("embedded", "http", "robot"), samples: int = 12) -> dict:
    """Replay one experiment across real adapters in a disposable scoped store.

    Measurements describe this single-process, single-caller fixture, not a
    managed-service load test or hardware result. No configured user store or
    remote deployment is read or mutated.
    """
    import asyncio
    import math
    import platform
    import secrets
    import tempfile
    import threading
    import time
    from collections import defaultdict
    from contextlib import ExitStack
    from dataclasses import replace
    from pathlib import Path

    from unimem import __version__, agent_registry, gateway, memory_authority
    from unimem.client import MemoryClient, learning_reply
    from unimem.memory_journal import Journal, close_connections
    from unimem.memory_record import MemoryRecord
    from unimem.robotics.bridge import GatewayRefused, HTTPGateway, RobotBridge
    from unimem.robotics.edge import OutcomeQueue

    allowed = {"embedded", "http", "robot", "mcp"}
    if not profiles or len(set(profiles)) != len(profiles) or set(profiles) - allowed:
        raise ValueError("profiles must be distinct members of embedded,http,robot,mcp")
    if type(samples) is not int or not 1 <= samples <= 100:
        raise ValueError("samples must be an integer from 1 to 100")
    checks: list[Result] = []
    timings: dict[str, list[float]] = defaultdict(list)
    refusals = 0
    disk_bytes = 0
    initialized_bytes = 0

    def verify(name: str, condition: bool) -> None:
        checks.append(Result("learning:" + name, bool(condition)))
        if not condition:
            raise ValueError("learning conformance failed: " + name)

    with tempfile.TemporaryDirectory(prefix="um-conf-") as root, ExitStack() as cleanup:
        cleanup.callback(close_connections, root)
        scope = {"agent": "conformance-robot"}
        source = MemoryRecord("memory", "fact", 1, {"text": "Check the calibration."}, scope=scope)
        journal = Journal(root)
        journal.put(source)
        journal.put(MemoryRecord("required", "fact", 1, {"text": "Respect the limit.", "protected": True}, scope=scope))
        journal.put(
            MemoryRecord("private", "fact", 1, {"text": "Outside the permitted scope."}, scope={"agent": "other"})
        )
        owner = MemoryClient(root, context=scope)
        configured = owner.learning.configure("observe", utility={"terms": {"completion": {"low": 0, "high": 100}}})
        experiment_id = configured["experiment_id"]
        credential = agent_registry.signup(root, scope["agent"])
        initialized_bytes = sum(path.stat().st_size for path in Path(root).rglob("*") if path.is_file())
        embedded = MemoryClient(root, agent_id=scope["agent"])
        callbacks: dict[str, Callable[[str, dict], dict]] = {"embedded": embedded.learning.call}
        robot = None
        transport = None
        queue = None
        try:
            if {"http", "robot"} & set(profiles):
                service = gateway.Gateway(root, token=secrets.token_hex(32))
                cleanup.callback(service.close)
                server = gateway.make_http_server(service, "127.0.0.1", 0)
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                cleanup.callback(server.server_close)
                cleanup.callback(worker.join, 5)
                cleanup.callback(server.shutdown)
                endpoint = f"http://127.0.0.1:{server.server_address[1]}"
                callbacks["http"] = MemoryClient(
                    url=endpoint, token=credential["token"], agent_id=scope["agent"]
                ).learning.call
                if "robot" in profiles:
                    transport = HTTPGateway(endpoint, credential["token"])
                    queue = OutcomeQueue(root)
                    robot = RobotBridge(
                        transport, embodiment="sim", site="lab", skill="pick", robot=scope["agent"], outcome_queue=queue
                    )

                    def robot_call(operation: str, payload: dict) -> dict:
                        assert robot is not None
                        args = {k: v for k, v in payload.items() if k != "task_context"}
                        return getattr(robot.learning, operation)(**args)

                    callbacks["robot"] = robot_call
            if "mcp" in profiles:
                try:
                    from unimem.mcp_server import build_server

                    mcp = build_server(root, allow_approval=False)
                except ImportError as exc:
                    raise ValueError("MCP profile requires the installed unimem[mcp] extra") from exc

                def mcp_call(operation: str, payload: dict) -> dict:
                    with memory_authority.restricted_writer(scope["agent"], "agent"):
                        response = asyncio.run(
                            mcp.call_tool("learning_" + operation, {**payload, "context": ["agent:" + scope["agent"]]})
                        )
                    data = getattr(response, "structured_content", None)
                    if data is None:
                        data = json.loads(response.content[0].text)
                    data = data.get("result", data)
                    if data.get("code") == "learning_conflict":
                        raise learning_contract.LearningConflict(data.get("detail", "conflict"))
                    if data.get("code") == "learning_unavailable":
                        raise learning_contract.LearningUnavailable(data.get("detail", "unavailable"))
                    return learning_reply(data)

                callbacks["mcp"] = mcp_call

            def invoke(profile: str, operation: str, payload: dict) -> dict:
                started = time.perf_counter()
                try:
                    return callbacks[profile](operation, payload)
                finally:
                    timings[profile + ":" + operation].append((time.perf_counter() - started) * 1000)

            def refused(profile: str, operation: str, payload: dict, statuses: tuple[int, ...]) -> bool:
                nonlocal refusals
                try:
                    invoke(profile, operation, payload)
                except (ValueError, ConnectionError, urllib.error.HTTPError) as exc:
                    status = getattr(exc, "status_code", getattr(exc, "status", getattr(exc, "code", None)))
                    if isinstance(exc, GatewayRefused):
                        status = exc.status
                    if status in statuses:
                        refusals += 1
                        return True
                    raise
                return False

            context = "sim/lab/pick"
            accepted = None
            for index in range(samples):
                payload = {
                    "occasion_id": f"episode-{index}",
                    "memory_ids": ["memory", "required"],
                    "task_context": context,
                    "experiment_id": experiment_id,
                }
                baseline = embedded.learning.call("assign", payload)
                for profile in profiles:
                    repeated = invoke(profile, "assign", payload)
                    verify(f"{profile}:assignment:{index}", repeated == baseline)
                    verify(
                        f"{profile}:protected:{index}",
                        next(row for row in repeated["arms"] if row["id"] == "required")["rate"] == 0,
                    )
                outcome = ({"succeeded": True}, {"reward": 0.75}, {"metrics": {"completion": 25}})[index % 3]
                receipt = {"occasion_id": payload["occasion_id"], "experiment_id": experiment_id}
                # A delayed receipt must not lose an outcome that arrived first.
                if index % 3 == 0:
                    owner.learning.call("outcome", {**receipt, **outcome})
                for profile in profiles:
                    delivered = invoke(profile, "delivery", {**receipt, "memory_ids": baseline["inject"]})
                    verify(f"{profile}:delivery:{index}", delivered["delivery"] == sorted(baseline["inject"]))
                    measured = invoke(profile, "outcome", {**receipt, **outcome})
                    expected = learning_contract.normalize_outcome(
                        configured["config"], payload["occasion_id"], {**receipt, **outcome}
                    )
                    verify(
                        f"{profile}:outcome:{index}",
                        measured["outcome"] == expected and measured["at"] == baseline["at"],
                    )
                    if index == 0:
                        accepted = measured
            for profile in profiles:
                status = invoke(profile, "status", {})
                verify(f"{profile}:exact-occasion-count", status["occasions"] == samples)
            report = owner.learning.run(experiment_id=experiment_id)
            verify("complete-evidence", report["evidence"] == samples and not report["applied"])
            close_connections(root)
            reopened = MemoryClient(root, context=scope)
            verify("reopen-acknowledged-evidence", reopened.learning.status()["occasions"] == samples)
            if queue is not None:
                offline = embedded.learning.assign(
                    "offline-episode", ["memory"], experiment_id=experiment_id, task_context=context
                )
                embedded.learning.delivery("offline-episode", offline["inject"], experiment_id=experiment_id)
                queue.enqueue(
                    {
                        "occasion_id": "offline-episode",
                        "experiment_id": experiment_id,
                        "metrics": {"completion": 25},
                        "context": ["agent:" + scope["agent"]],
                    },
                    path="/v1/learning/outcome",
                )
            changed = reopened.learning.configure("observe", utility={"terms": {"completion": {"low": 0, "high": 200}}})
            verify("new-configuration-boundary", changed["experiment_id"] != experiment_id)
            assert accepted is not None
            for profile in profiles:
                verify(
                    f"{profile}:scope-denial",
                    refused(
                        profile,
                        "assign",
                        {
                            "occasion_id": "scope-denial",
                            "memory_ids": ["private"],
                            "task_context": context,
                            "experiment_id": experiment_id,
                        },
                        (404,),
                    ),
                )
                late = invoke(
                    profile, "outcome", {"occasion_id": "episode-0", "experiment_id": experiment_id, "succeeded": True}
                )
                verify(f"{profile}:late-outcome-original-experiment", late == accepted)
                verify(
                    f"{profile}:conflicting-outcome",
                    refused(
                        profile,
                        "outcome",
                        {"occasion_id": "episode-0", "experiment_id": experiment_id, "succeeded": False},
                        (409,),
                    ),
                )
            if robot is not None and queue is not None and transport is not None:
                # Reopen an unacknowledged outcome and replay after reconfiguration.
                verify(
                    "robot:durable-queue-replay",
                    OutcomeQueue(root).flush(transport.post) == 1 and not queue.pending(),
                )
                replayed = reopened.learning.outcome(
                    "offline-episode", metrics={"completion": 25}, experiment_id=experiment_id
                )
                verify("robot:replayed-original-utility", replayed["outcome"]["reward"] == 0.25)
            journal.put(replace(source, version=2, payload={"text": "Use the corrected calibration."}))
            for profile in profiles:
                verify(
                    f"{profile}:corrected-source-refused",
                    refused(
                        profile,
                        "delivery",
                        {
                            "occasion_id": "episode-0",
                            "experiment_id": experiment_id,
                            "memory_ids": accepted["delivery"],
                        },
                        (409,),
                    ),
                )
            journal.forget("memory", reason="conformance erasure", scope=scope)
            for profile in profiles:
                verify(
                    f"{profile}:erased-source-refused",
                    refused(
                        profile,
                        "assign",
                        {
                            "occasion_id": "after-erasure",
                            "memory_ids": ["memory"],
                            "task_context": context,
                            "experiment_id": experiment_id,
                        },
                        (404,),
                    ),
                )
        except Exception as exc:
            checks.append(Result("learning:lifecycle", False, f"{type(exc).__name__}: {exc}"))
        finally:
            disk_bytes = sum(path.stat().st_size for path in Path(root).rglob("*") if path.is_file())
    measurements = []
    for identity, values in sorted(timings.items()):
        values.sort()
        measurements.append(
            {
                "adapter_operation": identity,
                "requests": len(values),
                **{
                    f"p{percent}_ms": round(values[max(0, math.ceil(len(values) * percent / 100) - 1)], 3)
                    for percent in (50, 95, 99)
                },
            }
        )
    peak_rss = None
    try:
        import resource

        peak_rss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * (1 if sys.platform == "darwin" else 1024)
    except ImportError:
        pass  # Windows has no resource module; an absent measurement stays explicit.
    return {
        "schema": "unimem.learning-conformance.v1",
        "passed": bool(checks) and all(row.ok for row in checks),
        "profiles": list(profiles),
        "versions": {"unimem": __version__, "python": platform.python_version()},
        "environment": {"platform": platform.platform(), "machine": platform.machine(), "logical_cpus": os.cpu_count()},
        "workload": {
            "episodes": samples,
            "offline_replay_episodes": int("robot" in profiles),
            "canonical_sources": 3,
            "concurrency": 1,
            "storage": "temporary SQLite",
            "network": "loopback HTTP" if {"http", "robot"} & set(profiles) else "none",
        },
        "checks": to_dict(checks),
        "measurements": measurements,
        "expected_refusals": refusals,
        "store_bytes_before_cleanup": disk_bytes,
        "store_bytes_after_initialization": initialized_bytes,
        "store_growth_bytes": disk_bytes - initialized_bytes,
        "process_lifetime_peak_rss_bytes": peak_rss,
        "capabilities": learning_contract.capabilities(),
        "unvalidated": [
            "managed-deployment load and recovery",
            "physical robot hardware",
            "external SDK implementations",
            "task-value gains",
        ],
    }


def render(results: list[Result]) -> str:
    lines = [f"{'PASS' if r.ok else 'FAIL'}  {r.name}" + (f"  ({r.detail})" if r.detail else "") for r in results]
    ok = all(r.ok for r in results)
    lines.append(
        f"\n{sum(r.ok for r in results)}/{len(results)} checks passed: "
        + ("conformant at the layers checked." if ok else "NOT conformant.")
    )
    lines.append(NOT_TESTED)
    return "\n".join(lines)


def main_reference() -> int:
    """`python -m unimem.conformance`: the reference stdio implementation, for trying the runner."""
    for line in sys.stdin:
        if line.strip():
            print(json.dumps(answer(json.loads(line))), flush=True)
    return 0


def to_dict(results: list[Result]) -> list[dict]:
    return [dataclasses.asdict(r) for r in results]


if __name__ == "__main__":
    sys.exit(main_reference())
