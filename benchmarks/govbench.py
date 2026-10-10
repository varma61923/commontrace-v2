"""GovBench: multi-principal memory governance -- utility, leakage and verified forgetting.

Shared assistants (a hospital ward, an office, a household) have many principals
writing to one memory and reading it under different entitlements. GateMem
(arXiv 2606.18829) and PiSAs (arXiv 2607.05318) found that no tested system
achieves utility, access control and forgetting at once. GovBench measures the
three through CommonTrace's real gateway:

* **Utility**: every principal can recall each memory it is entitled to.
* **Leakage**: no principal ever receives a memory it is not entitled to --
  another agent's private notes, another patient's record, another team's data.
* **Forgetting**: after the operator forgets records, no principal receives them
  or anything derived from them through any recall surface, and a signed
  forgetting certificate names every blocked record.

Each check runs through ``/v1/memory/profile``, ``/v1/memory/search`` and
``/v1/memory/reflect`` with real agent credentials minted by ``/v1/agent/signup``.

    python -m benchmarks.govbench --out govbench.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile

SURFACES = ("profile", "search", "reflect")
QUERY = "ward update"

PRINCIPALS = {
    "doctor-1": ["project:ward-3", "user:patient-a", "user:patient-b"],
    "nurse-1": ["project:ward-3", "user:patient-a"],
    "nurse-2": ["project:ward-3", "user:patient-b"],
    "billing-1": ["project:billing"],
}

# (marker, labels, writer) -- writer "operator" writes with the owner key and these labels;
# an agent id writes its own private note through its own credential.
MEMORIES = [
    ("M01", ["project:ward-3"], "operator"),
    ("M02", ["project:ward-3", "user:patient-a"], "operator"),
    ("M03", ["project:ward-3", "user:patient-b"], "operator"),
    ("M04", ["project:billing"], "operator"),
    ("M05", ["project:billing", "user:patient-a"], "operator"),
    ("M06", None, "nurse-1"),
    ("M07", None, "doctor-1"),
    ("M08", None, "billing-1"),
]
FORGET = ("M02", "M04")


def _entitled(principal: str, labels: list[str] | None, writer: str) -> bool:
    if labels is None:  # a private agent note
        return writer == principal
    return set(labels) <= {"agent:" + principal, *PRINCIPALS[principal]}


def _text(marker: str) -> str:
    return f"Ward update {marker}: status note {hashlib.sha256(marker.encode()).hexdigest()[:8]}."


class _Client:
    def __init__(self, gateway, token):
        self.gateway, self.token = gateway, token

    def post(self, path: str, body: dict) -> dict:
        response = self.gateway.handle("POST", path, {"Authorization": "Bearer " + self.token,
                                                      "Host": "localhost"}, json.dumps(body).encode())
        payload = json.loads(response.body)
        if response.status != 200:
            raise RuntimeError(f"{path} -> {response.status}: {payload}")
        return payload


def _recall(client: _Client, surface: str) -> str:
    if surface == "profile":
        return json.dumps(client.post("/v1/memory/profile", {"query": QUERY, "limit": 100}))
    if surface == "search":
        return json.dumps(client.post("/v1/memory/search", {"query": QUERY, "limit": 100}))
    return json.dumps(client.post("/v1/memory/reflect", {"query": QUERY, "budget": 8000}))


def run() -> dict:
    from commontrace import gateway as gateway_mod
    from commontrace import hierarchical, holdout_io, memory_authority

    root = tempfile.mkdtemp(prefix="govbench-")
    try:
        holdout_io.configure(root, rate=0.0001, salt="govbench")
        owner = "o" * 40
        gw = gateway_mod.Gateway(root, token=owner)
        operator = _Client(gw, owner)
        agents = {}
        for principal, labels in PRINCIPALS.items():
            minted = operator.post("/v1/agent/signup", {"agent_id": principal, "scopes": labels})
            agents[principal] = _Client(gw, minted["token"])
        fact_ids = {}
        for marker, labels, writer in MEMORIES:
            client = operator if writer == "operator" else agents[writer]
            body = {"text": _text(marker)}
            if writer == "operator":
                body["context"] = labels
            added = client.post("/v1/memory/add", body)
            fact_ids[marker] = added["facts"][0]["id"]
        # A summary derived from a record that will be forgotten: forgetting must follow the lineage.
        with memory_authority.writer("summarizer", "operator"):
            [(derived, _)] = hierarchical.append_facts(root, [{
                "statement": "Ward update M09 (summary of M02): stable overnight.",
                "scopes": ["project:ward-3", "user:patient-a"], "source_trace_id": fact_ids["M02"]}])
        fact_ids["M09"] = derived.id
        memories = MEMORIES + [("M09", ["project:ward-3", "user:patient-a"], "operator")]

        def measure(forgotten: set[str]) -> dict:
            rows = []
            for principal, client in agents.items():
                for surface in SURFACES:
                    seen = _recall(client, surface)
                    for marker, labels, writer in memories:
                        rows.append({"principal": principal, "surface": surface, "memory": marker,
                                     "entitled": _entitled(principal, labels, writer) and marker not in forgotten,
                                     "forgotten": marker in forgotten, "delivered": marker in seen})
            return rows

        before = measure(set())
        for marker in FORGET:
            hierarchical.forget_fact(root, fact_ids[marker])
        forgotten = set(FORGET) | {"M09"}
        after = measure(forgotten)
        certificate = memory_authority.forgetting_certificate(root, fact_ids["M02"])
        certified = set(certificate["blocked_records"])
    finally:
        shutil.rmtree(root, ignore_errors=True)

    def score(rows):
        entitled = [r for r in rows if r["entitled"]]
        unentitled = [r for r in rows if not r["entitled"] and not r["forgotten"]]
        forgotten_rows = [r for r in rows if r["forgotten"]]
        return {"utility": sum(r["delivered"] for r in entitled) / len(entitled) if entitled else None,
                "leaks": sum(r["delivered"] for r in unentitled),
                "unentitled_checks": len(unentitled),
                "forgotten_delivered": sum(r["delivered"] for r in forgotten_rows),
                "forgotten_checks": len(forgotten_rows)}

    with open(os.path.abspath(__file__), "rb") as fh:
        source = hashlib.sha256(fh.read()).hexdigest()
    return {"benchmark": "GovBench", "version": 1, "source_sha256": source,
            "principals": PRINCIPALS, "surfaces": list(SURFACES),
            "before_forgetting": score(before), "after_forgetting": score(after),
            "certificate_covers_derived": fact_ids["M09"] in certified and fact_ids["M02"] in certified,
            "rows": {"before": before, "after": after},
            "note": "One synthetic clinic deployment through the local gateway's agent credentials. "
                    "Forgetting covers local recall surfaces, not historical bytes or remote replicas."}


def render(report: dict) -> str:
    b, a = report["before_forgetting"], report["after_forgetting"]
    return "\n".join([
        f"GovBench v{report['version']} -- {len(report['principals'])} principals x "
        f"{len(report['surfaces'])} recall surfaces", "",
        "| phase | utility | leaks / checks | forgotten delivered / checks |",
        "| --- | --: | --: | --: |",
        f"| before forgetting | {b['utility']:.0%} | {b['leaks']} / {b['unentitled_checks']} | - |",
        f"| after forgetting | {a['utility']:.0%} | {a['leaks']} / {a['unentitled_checks']} | "
        f"{a['forgotten_delivered']} / {a['forgotten_checks']} |",
        "", f"Forgetting certificate covers the derived summary: {report['certificate_covers_derived']}",
        "", report["note"]])


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--out", default=None)
    args = p.parse_args(argv)
    report = run()
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
    print(render(report))
    ok = (report["before_forgetting"]["leaks"] == 0 and report["after_forgetting"]["leaks"] == 0
          and report["after_forgetting"]["forgotten_delivered"] == 0)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
