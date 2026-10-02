"""The CommonTrace conformance suite: what it takes to call an implementation "CommonTrace-compatible"."""
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

from commontrace import experiment, frontmatter, paths, raw_export, revision, validate, value

SUITE_VERSION = "1"
VECTORS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "conformance_vectors.json")
SPEC_VECTORS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "protocol",
                                 "conformance", "vectors.json")
NOT_TESTED = ("what is not tested: statistics (intervals, sequential validity), the integrity audit, retrieval "
              "ranking, and anything about how outcomes are obtained.")


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
        row = value._FIELD_SEP.join((str(index), r["slug"], r["verdict"], f"{r['occasions_improved']:.6f}",
                                     f"{r['rate']:.6f}", f"{money:.6f}"))
        previous = hashlib.sha256((previous + value._FIELD_SEP + row).encode("utf-8")).hexdigest()
        out.append(previous)
    return out


def ref_digest(rows: list[list[str]]) -> str:
    payload = raw_export._DIGEST_DOMAIN + "\x1e" + "\x1e".join(sorted("\x1f".join(r) for r in rows))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def ref_revision(fm: dict, body: str) -> str:
    return revision.revision_of(fm, body)


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
    return {"error": f"unknown op {op!r}"}


def build_vectors() -> dict:
    """Generate the reference vectors. Deterministic: the same code gives the same file."""
    rng = random.Random("commontrace-conformance-v1")
    assign = []
    names = ["lesson-a", "refund-policy", "grasp/soft-cup", "ünïcode-leçon", "x" * 80, "a b", "a\x1fb"]
    for i in range(160):
        lesson = rng.choice(names)
        occasion = rng.choice([f"ep-{rng.randint(0, 10**6)}", f"robot-{rng.randint(1, 9)}/ep-{i}", "ocčasion-ß",
                               str(i)])
        salt = rng.choice(["default", "salt-1", "exp-2026-q1", "ünï"])
        rate = rng.choice([0.0, 0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0])
        assign.append({"lesson": lesson, "occasion": occasion, "salt": salt, "rate": rate,
                       "held_out": ref_assign(lesson, occasion, salt, rate)})
    ledgers = []
    for n in (0, 1, 3, 6):
        rows = [{"slug": f"memory-{k}", "verdict": rng.choice(["HELPS", "HURTS"]),
                 "occasions_improved": round(rng.uniform(-40, 400), 3), "rate": round(rng.uniform(1, 80), 2)}
                for k in range(n)]
        ledgers.append({"rows": rows, "hashes": ref_ledger(rows)})
    digests = []
    for n in (0, 1, 5, 40):
        rows = [[f"m{rng.randint(0, 3)}", f"o{k}", rng.choice(["injected", "withheld"]),
                 rng.choice(["true", "false", ""]), "2026-01-01T00:00:00+00:00", "", "r1", "0.5", "salt-1"]
                for k in range(n)]
        digests.append({"rows": rows, "digest": ref_digest(rows)})
    revisions = []
    for fm, body in (
            ({"applies_when": "when  a  refund  is asked", "do_not_apply_when": "never", "tags": ["b", "a"]},
             "## Rule\nLink the policy.\n"),
            ({"applies_when": "when a refund is asked", "do_not_apply_when": "never", "tags": ["a", "b"]},
             "## Rule\r\nLink the policy.\r\n\r\n\r\n"),
            ({"applies_when": "ünï", "do_not_apply_when": "", "tags": []}, "")):
        revisions.append({"frontmatter": fm, "body": body, "revision": ref_revision(fm, body)})
    return {"suite": SUITE_VERSION, "protocol": "2.0.0", "assign": assign, "ledger": ledgers, "digest": digests,
            "revision": revisions}


def load_vectors(path: str | None = None) -> dict:
    with open(path or VECTORS_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def run_exec(command: str, vectors: dict, timeout: float = 30.0, only: tuple[str, ...] | None = None) -> list[Result]:
    """Run `command` as a stdio program against every vector. One process, one JSON object per line."""
    ops = only or ("assign", "ledger", "digest", "revision")
    vectors = {k: (v if k in ops else []) for k, v in vectors.items() if isinstance(v, list)}
    requests, expected = [], []
    for v in vectors["assign"]:
        requests.append({"op": "assign", "lesson": v["lesson"], "occasion": v["occasion"], "salt": v["salt"],
                         "rate": v["rate"]})
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
    try:
        argv = shlex.split(command)
        if not argv:
            return [Result("exec", False, "no command given")]
        proc = subprocess.run(argv, input="".join(json.dumps(r) + "\n" for r in requests),  # nosec B603
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return [Result("exec", False, f"no answer within {timeout:g}s")]
    except OSError as exc:
        return [Result("exec", False, f"cannot run {command!r}: {exc}")]
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if len(lines) != len(requests):
        return [Result("exec", False, f"sent {len(requests)} requests, got {len(lines)} answers"
                       + (f"; stderr: {proc.stderr.strip()[:200]}" if proc.stderr.strip() else ""))]
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
                f"{json.dumps(request)[:120]} -> {line[:120]}, expected {json.dumps(want)[:80]}")
    return [Result(f"vectors:{op}", op not in failures,
                   f"{totals[op] - len(failures.get(op, []))}/{totals[op]} agree"
                   + (f"; first miss: {failures[op][0]}" if op in failures else ""))
            for op in ("assign", "ledger", "digest", "revision") if op in ops]


def check_vectors_against_reference(vectors: dict) -> list[Result]:
    """The committed file must equal what the reference implementation produces: the file is the contract."""
    fresh = build_vectors()
    return [Result(f"reference:{k}", vectors.get(k) == fresh[k], "the committed vectors match the reference"
                   if vectors.get(k) == fresh[k] else "the committed vectors differ from the reference implementation")
            for k in ("assign", "ledger", "digest", "revision")]


def check_store(root: str) -> list[Result]:
    out: list[Result] = []
    for kind, directory, schema_name in (("trace", paths.traces_dir(root), "trace.schema.json"),
                                         ("lesson", paths.lessons_dir(root), "lesson.schema.json")):
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
    from commontrace import holdout_io

    rows, corrupt = holdout_io.read_log(root)
    out.append(Result("store:assignment log readable", corrupt == 0,
                      f"{len(rows)} assignments" + (f"; {corrupt} unreadable line(s)" if corrupt else "")))
    wrong = [r for r in rows if r.salt is not None and r.rate is not None
             and (ref_assign(r.lesson, r.occasion_id, r.salt, r.rate) == bool(r.injected))]
    out.append(Result("store:assignments follow the randomization", not wrong,
                      f"{len(rows)} recomputed" + (f"; first miss: {wrong[0].lesson} on {wrong[0].occasion_id}"
                                                   if wrong else "")))
    return out


def _call(base: str, method: str, path: str, token: str | None, body: dict | None = None) -> tuple[int, dict]:
    if urllib.parse.urlsplit(base).scheme not in ("http", "https"):
        return 0, {"error": "the gateway URL must be http or https"}
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(base.rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:  # nosec B310 - scheme checked above
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}
    except (urllib.error.URLError, OSError) as exc:
        return 0, {"error": str(exc)}


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
    delivered = {x["id"] if isinstance(x, dict) else x for x in first.get("deliver", [])}
    withheld = set(first.get("withheld", []))
    candidates = {i["id"] for i in items}
    add("gateway:recall partitions the candidates", delivered & withheld == set() and
        candidates <= (delivered | withheld), f"delivered {len(delivered)}, withheld {len(withheld)}")
    add("gateway:a protected memory is always delivered", protected["id"] in delivered and
        protected["id"] not in withheld)
    _, second = _call(base, "POST", "/v1/recall", token, {"occasion_id": occasion, "items": [*items, protected]})
    again = {x["id"] if isinstance(x, dict) else x for x in second.get("deliver", [])}
    add("gateway:the same occasion gets the same partition", again == delivered)

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


def render(results: list[Result]) -> str:
    lines = [f"{'PASS' if r.ok else 'FAIL'}  {r.name}" + (f"  ({r.detail})" if r.detail else "") for r in results]
    ok = all(r.ok for r in results)
    lines.append(f"\n{sum(r.ok for r in results)}/{len(results)} checks passed: "
                 + ("conformant at the layers checked." if ok else "NOT conformant."))
    lines.append(NOT_TESTED)
    return "\n".join(lines)


def main_reference() -> int:
    """`python -m commontrace.conformance`: the reference stdio implementation, for trying the runner."""
    for line in sys.stdin:
        if line.strip():
            print(json.dumps(answer(json.loads(line))), flush=True)
    return 0


def to_dict(results: list[Result]) -> list[dict]:
    return [dataclasses.asdict(r) for r in results]


if __name__ == "__main__":
    sys.exit(main_reference())
