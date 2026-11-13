"""Offline execution checks for every feature shipped in UniMem vertical packs.

These workflows use temporary stores and real library entry points. Their
planted scenarios check local behavior; they do not measure field efficacy.
Synthetic outcome estimates are produced separately by the pack runner.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import random
import secrets
import shutil
import tempfile
import time
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypedDict

from unimem import frontier_protocol as fp
from unimem import offline

Scalar = bool | str | int | float


class _Episode(TypedDict):
    episode: str
    propensity: int


def _checked(condition: bool, detail: str, **metrics: Scalar) -> dict[str, Scalar]:
    if not condition:
        raise RuntimeError("UniMem offline feature check failed: " + detail)
    return {"passed": True, "detail": detail, **metrics}


def _fact(root: str) -> str:
    from unimem import hierarchical

    fact = hierarchical.append_facts(root, [{"statement": "Verify the reviewed local procedure before acting."}])[0][0]
    return fp.text(fact.id, "fact id", 256)


class _World:
    def rollout(self, state: dict[str, Any], action: dict[str, Any]) -> dict[str, Any]:
        force = fp.number(action["force"], 0, 10)
        return {"safe": force < 5, "distance": force * 2}


def _world(root: str, seed: int) -> tuple[str, str, _World]:
    from unimem import world_model_trust

    identity = _fact(root)
    digest = fp.digest(["unimem-offline-world", seed])
    world_model_trust.register(root, "demo-world", digest, embodiment="demo-arm", tasks=["place"], adapter="local")
    return identity, digest, _World()


def _case(feature: str, root: str, seed: int) -> dict[str, Scalar]:
    if feature == "G1":
        from unimem import skill_foundry

        value = skill_foundry.candidate(
            [{"solution_text": "Verify the signature before release."}],
            name="verify-release",
            description="Check a release signature.",
        )
        screened = skill_foundry.prescreen(
            value,
            [
                {
                    "trace_id": "failure",
                    "instruction": "Verify release.",
                    "verifier": {"contains": "signature"},
                    "passing": "Verify signature.",
                    "failing": "Skip review.",
                }
            ],
        )
        return _checked(
            screened["passed"]
            and not skill_foundry.exposed(value, False)
            and len(skill_foundry.exposed(value, True)) == 1,
            "Skill induction, verifier and catalog exposure",
        )
    if feature == "G2":
        from unimem import rl_export

        base = {
            "unit": "memory",
            "cluster": "release",
            "context": "coding",
            "prompt": "Verify release.",
            "scopes": [],
            "integrity": "operator",
            "confidentiality": "public",
            "rate": 0.5,
        }
        rows = [
            {
                **base,
                "occasion_id": "treated",
                "trace_id": "t",
                "response": "Verified.",
                "injected": True,
                "outcome": True,
            },
            {
                **base,
                "occasion_id": "control",
                "trace_id": "c",
                "response": "Unchecked.",
                "injected": False,
                "outcome": False,
            },
        ]
        pairs = rl_export.preferences(rows, {"memory": {"effect": 0.2, "interval": [-0.1, 0.5]}})
        return _checked(
            len(pairs) == 1 and pairs[0]["meta"]["same_instance_counterfactual"] is False,
            "Matched randomized-arm training schema; synthetic fixture, not a same-instance counterfactual",
        )
    if feature == "G3":
        from unimem import optimizer_intake

        proposal = optimizer_intake.candidate(
            {
                "base_artifact": "release",
                "delta": "Verify the signature.",
                "optimizer": "generic",
                "claimed_validation_score": 999,
                "parent": None,
            }
        )
        frontier = optimizer_intake.pareto(
            [
                {"id": proposal["id"], "effect": 0, "harm": 0.2, "supported": True},
                {"id": "supported", "effect": 0.2, "harm": 0.1, "supported": True},
            ]
        )
        return _checked(frontier == ["supported"], "Optimizer claims cannot replace measured frontier coordinates")
    if feature == "G4":
        from unimem import sentinel

        rows = [
            {
                "occasion_id": str(i),
                "update": bool(i % 2),
                "canary": bool(i // 2 % 2),
                "outcome": int(bool(i // 2 % 2) != bool(i % 2)),
                "update_rate": 0.5,
                "canary_rate": 0.5,
            }
            for i in range(160)
        ]
        report = sentinel.interference(rows, tolerance=0.1, minimum_support=5)
        return _checked(
            report["regression"] and not report["allow"],
            "Planted factorial forgetting update is blocked",
            estimated_interaction=report["estimate"],
        )
    if feature == "G5":
        from unimem import sycophancy

        rows = [
            {
                "occasion_id": str(i),
                "query_id": "query",
                "injected": bool(i % 2),
                "rate": 0.5,
                "outcome": 1,
                "accuracy": 1,
                "agreement": int(bool(i % 2)),
            }
            for i in range(40)
        ]
        report = sycophancy.audit(rows, horizon=800, queries=["query"])
        return _checked(
            not report["decision_ready"] and not report["flattering"],
            "Short personalization cohorts remain explicitly underpowered",
        )
    if feature == "G6":
        from unimem import capabilities

        now = fp.now()
        keys = {name: secrets.token_bytes(32) for name in ("issuer", "agent-a", "agent-b")}
        caveats = {
            "namespaces": ["facts"],
            "operations": ["read", "write"],
            "classes": ["general"],
            "purposes": ["support"],
            "labels": [],
            "delegates": ["agent-b"],
            "ceiling": 1,
            "audience": "agent-a",
            "expires_at": (datetime.fromisoformat(now) + timedelta(hours=1)).isoformat(),
        }
        token = capabilities.grant(
            caveats,
            issuer="operator",
            tenant="demo",
            nonce="demo",
            now=now,
            algorithm="hmac-sha256",
            key=keys["issuer"],
        )
        child = capabilities.attenuate(
            token, {**caveats, "audience": "agent-b", "delegates": [], "operations": ["read"]}, key=keys["agent-a"]
        )
        verified = capabilities.verify(
            child,
            issuer="operator",
            tenant="demo",
            algorithm="hmac-sha256",
            key=keys["issuer"],
            holder_key=keys.__getitem__,
            now=now,
        )
        return _checked(verified["caveats"]["operations"] == ["read"], "Holder-authenticated capability attenuation")
    if feature == "G7":
        from unimem import team_isolation

        old = team_isolation.transaction_time({"id": "fact", "statement": "old"})
        new = team_isolation.transaction_time({"id": "fact", "statement": "new"})
        declared = team_isolation.read_set([{"id": "fact", "transaction_time": old}])
        return _checked(
            team_isolation.stale(declared, {"fact": new}) == ["fact"],
            "Declared stale fact versions are detected before team commit",
        )
    if feature == "G8":
        from unimem import contagion

        identity = _fact(root)
        report = contagion.independence(root, [identity], downweight=True)
        return _checked(
            report["n_eff"] == 1 and len(report["records"]) == 1,
            "Admitted lineage roots drive independent-source annotations",
            n_eff=report["n_eff"],
        )
    if feature == "G9":
        from unimem import holdout_io, writer_holdout

        holdout_io.configure(root, rate=0.5, salt="unimem-pack-writer")
        writer_holdout.register(root, "demo-writer", training_size=10, minimum_support=2)
        report = writer_holdout.reliability(root, "demo-writer")
        return _checked(
            report["n"] == 0 and report["score"] == 0,
            "Preregistered writer ledger does not invent reliability without outcomes",
        )
    if feature == "G10":
        from unimem import otel_memory

        identity = _fact(root)
        attrs = otel_memory.attributes(root, "search", [identity])
        return _checked(
            attrs["gen_ai.memory.record.id"] == identity
            and not any("contents" in name or "query.text" in name for name in attrs),
            "Admitted standard memory attributes omit query and record contents by default",
        )
    if feature == "G11":
        from unimem import zk_effect

        hiding = fp.digest(["demo-hiding", seed])
        entries: list[_Episode] = [{"episode": f"episode-{i:03d}", "propensity": 500_000} for i in range(20)]
        plan = zk_effect.plan("demo", entries, commitment_key=zk_effect.seed_key(hiding))
        challenge = zk_effect.challenge(plan, fp.digest(["demo-verifier", seed]))
        rows = []
        for cohort_entry in entries:
            assigned = zk_effect.assignment(plan, challenge, cohort_entry["episode"], cohort_entry["propensity"])
            rows.append({**cohort_entry, "assigned": assigned, "outcome": 1_000_000 if assigned else 0})
        witness = {"plan": plan, "challenge": challenge, "rows": rows, "commitment_seed": hiding, "noise_seed": None}
        proof = zk_effect.transparent(witness)
        return _checked(
            zk_effect.verify_transparent(proof, witness, fp.digest(challenge)),
            "Complete synthetic witness recomputed; this backend provides no input privacy",
            backend=proof["backend"],
            private_rows=proof["private_rows"],
        )
    if feature == "G12":
        from unimem import attested_keys

        build = {
            "backend": "simulated",
            "claims": {"test_build": "offline-demo"},
            "description": "Synthetic verifier; no hardware isolation",
        }
        registered = attested_keys.register(root, build)
        request = attested_keys.issue(
            root,
            "demo",
            registered["measurement"],
            fp.digest(["worker", seed]),
            payload_digest=fp.digest(["payload", seed]),
        )
        verifier = attested_keys.SimulatedVerifier(secrets.token_bytes(64))
        quote = verifier.quote(request, build)
        attested_keys.verify_quote(request, quote, build, verifier, allow_simulated=True)
        return _checked(
            True,
            "Registered measurement and request-bound simulated quote verified",
            simulated=True,
            hardware_isolation=False,
        )
    if feature == "G13":
        from unimem import oblivious

        centroids = {"public": [0.0]}
        blocks = oblivious.cluster_records(
            [
                {"id": "allowed", "label": "public", "vector": [0.1]},
                {"id": "hidden", "label": "restricted", "vector": [0.0]},
            ],
            centroids,
            clearance="public",
            block_bytes=256,
            blocks_per_cluster=2,
        )
        replicas = [oblivious.XorPIRReplica(blocks, block_bytes=256) for _ in range(2)]

        def fetch(cluster: str, first: Any, second: Any) -> Any:
            if first is None:
                return replicas[0].handle(cluster, None)
            return replicas[0].handle_many(cluster, first), replicas[1].handle_many(cluster, second)

        result = oblivious.client_recall(centroids, [0.0], clearance="public", limit=2, fetch_pair=fetch)
        return _checked(
            [row["id"] for row in result["results"]] == ["allowed"],
            "Two local PIR replicas return only the authorized database",
        )
    if feature == "G14":
        from unimem import policy_proof

        domain = {
            "principals": [{"id": "agent", "class": "support", "attributes": {}}],
            "actions": list(policy_proof.ACTIONS),
            "memory_classes": ["general"],
            "label_sets": [[]],
            "purposes": ["support"],
        }
        prior_policy = policy_proof.deny(domain)
        changed_policy = {**prior_policy, "rules": [{"id": "read", "effect": "allow", "actions": ["read"]}]}
        widened = policy_proof.difference(prior_policy, changed_policy)
        return _checked(
            not widened["no_widening"] and policy_proof.difference(prior_policy, prior_policy)["no_widening"],
            "Finite-domain widening is detected; unchanged policy is certified without widening",
            widening_count=widened["widening_count"],
        )
    if feature == "G15":
        from unimem import passport

        now = fp.now()
        entry = {
            "parent_ids": [],
            "created_at": now,
            "version": "1",
            "content": {"id": "fact", "statement": "Reviewed limit."},
            "provenance": {"source_id": "fact", "namespace": "facts", "origin": {}},
            "confidence": 0.9,
            "timestamps": {"valid_from": now, "valid_until": None},
            "consent_scope": {"status": "not-configured", "purposes": []},
            "labels": [],
            "capability_tokens": [],
        }
        manifest = passport.build(
            {"semantic": [entry]},
            created_at=now,
            source_agent={
                "name": "demo",
                "runtime": "unimem",
                "model_digest": "sha256:" + fp.digest(["model", seed]),
                "model_identity": "operator-declared digest; execution not attested",
            },
        )
        signed = fp.sign(root, manifest)
        return _checked(
            passport.verify(manifest) == manifest and fp.verify(root, signed),
            "Merkle passport round-trips with a local HMAC envelope; not a public-key credential",
        )
    if feature == "G16":
        from unimem import nextwave_store, purpose

        identity = _fact(root)
        health, shopping = purpose.DPV + "ImproveHealthcare", purpose.DPV + "ServiceProvision"
        purpose.consent(
            root,
            "person@demo.invalid",
            [identity],
            [health],
            controller="Synthetic clinic",
            expires_at=(datetime.fromisoformat(fp.now()) + timedelta(hours=1)).isoformat(),
            collection_method="Synthetic affirmative consent",
        )
        with purpose.request(root, shopping):
            hidden = identity not in nextwave_store.admit(root, [identity])
        with purpose.request(root, health):
            visible = identity in nextwave_store.admit(root, [identity])
        return _checked(hidden and visible, "Consented health memory is unavailable to a shopping purpose")
    if feature == "G17":
        from unimem import household

        calls: list[object] = []
        result = household.observe(
            root,
            {"object": "person"},
            room_id="kitchen",
            identity_resolver=lambda scene: [{"status": "unknown"}],
            persist=lambda *args: calls.append(args),
        )
        return _checked(not result["persisted"] and not calls, "Unknown bystanders never reach persistence")
    if feature == "G18":
        from unimem import subject_policy
        from unimem.memory_journal import Journal
        from unimem.memory_record import MemoryRecord

        subject_policy.compile(
            root, [{"id": "workplace", "scopes": ["project:demo"], "subject_class": "adult", "context": "workplace"}]
        )
        record = MemoryRecord(
            "emotion",
            "observation",
            1,
            {"text": "Synthetic inferred emotional state."},
            provenance={"source": {"modality": "voice", "labels": ["emotion"], "inferred": True}},
            scope={"labels": ["project:demo"]},
        )
        try:
            Journal(root).put(record)
        except subject_policy.HeldForReview:
            decision = subject_policy.explain(root)["decisions"][-1]["decision"]
            return _checked(
                decision == "quarantine", "Biometric emotion inference is quarantined before a workplace write"
            )
        raise RuntimeError("UniMem regulated write was unexpectedly admitted")
    if feature == "G19":
        from unimem import kv_capsules

        identity, model = _fact(root), fp.digest(["capsule-model", seed])
        kv_capsules.build(
            root, identity, model, b"synthetic-kv", promotion_score=0.8, promotion_threshold=0.5, tiers=("memory",)
        )
        try:
            content = kv_capsules.get(root, identity, model)
        finally:
            removed = kv_capsules.evict(root, identity, reason="quarantine")
        return _checked(
            content == b"synthetic-kv" and removed["removed_tiers"] == ["memory"],
            "Model-bound reference capsule round-trips and is evicted; promotion score is a fixture input",
        )
    if feature == "G20":
        from unimem import change_stream

        identity = _fact(root)
        change_stream.subscribe(root, "demo-reader")
        change_stream.publish(root, "assert", identity, namespace="facts")
        first = change_stream.poll(root, "demo-reader")
        resumed = change_stream.poll(root, "demo-reader", cursor=first["cursor"])
        return _checked(
            any(event["id"] == identity for event in first["events"]) and not resumed["events"],
            "An admitted change is delivered and its resume cursor does not repeat it",
        )
    if feature == "G21":
        from unimem import tiering

        identity = _fact(root)
        tiering.configure(root, "demo", region="local", allowed_regions=["local"], cold_region="local")
        tiering.put(root, "demo", "payload", {"frames": [1, 2, 3]}, region="local", sources=[identity])
        tiering.hibernate(root, "demo", region="local")
        tiering.prewarm(root, "demo", region="local", trigger="robot-boot")
        value = tiering.get(root, "demo", "payload", region="local")
        return _checked(
            value == {"frames": [1, 2, 3]}, "Admitted local tier hibernation and prewarm preserve payload bytes"
        )
    if feature == "G22":
        from unimem import human_help

        guarded = human_help.should_ask(root, "demo", "clarify", "occasion")
        report = human_help.plan(
            [
                {"id": "low", "risk": "low-risk", "modes": [{"mode": "clarify", "seconds": 5, "value": 0.6}]},
                {"id": "safety", "risk": "high-risk", "modes": []},
            ],
            budget_seconds=5,
        )
        return _checked(
            not guarded["randomized"]
            and report["reserved_seconds"] <= 5
            and report["decisions"][1]["mode"] == "safe-escalation",
            "Unconfigured asking never randomizes, and the budget planner preserves safety escalation",
        )
    if feature == "G23":
        from unimem import world_model_trust

        identity, digest, adapter = _world(root, seed)
        report = world_model_trust.gate(
            root,
            identity,
            model_digest=digest,
            embodiment="demo-arm",
            task="place",
            state={"surface": "counter"},
            action={"force": 2},
            perturbations=[{"force": 3}],
            adapter=adapter,
        )
        return _checked(
            report["decision"] == "hold" and not report["trust"]["trusted_for_veto"],
            "An action-sensitive synthetic model cannot promote without real-outcome trust",
        )
    if feature == "G24":
        from unimem.robotics import signatures

        samples = [
            {
                "timestamp": f"2026-01-01T00:00:{i:02d}+00:00",
                "episode_id": "nominal",
                "phase": "lift",
                "payload": "light",
                "firmware": "v1",
                "signals": {"force_torque": 1.0},
            }
            for i in range(30)
        ]
        signatures.build(root, samples, skill="lift", embodiment="demo-arm", site="demo")
        result = signatures.check(
            root,
            {**samples[-1], "episode_id": "anomaly", "signals": {"force_torque": 100.0}},
            skill="lift",
            embodiment="demo-arm",
            site="demo",
            link_recovery=False,
        )
        return _checked(
            result["anomaly"] and bool(result.get("memory_id")),
            "Planted force exceedance becomes an episode-linked anomaly memory",
        )
    if feature == "G25":
        from unimem.robotics.working_memory import WorkingMemory

        memory = WorkingMemory(capacity_tokens=1)
        memory.add("safe", [1, 0], tokens=1, episode_id="demo", safety=True)
        memory.add("ordinary", [0, 1], tokens=1, episode_id="demo")
        result = memory.retrieve([1, 0], budget_ms=100)
        return _checked(
            result["result"] is not None and result["result"]["id"] == "safe",
            "Safety keyframe survives local eviction pressure and is recalled without network I/O",
        )
    if feature == "G26":
        from unimem.robotics import site_memory

        message = {
            "headerId": 1,
            "timestamp": "2026-01-01T00:00:00Z",
            "version": "3.0.0",
            "manufacturer": "demo",
            "serialNumber": "robot",
            "orderId": "order",
            "orderUpdateId": 1,
            "nodeStates": [{"nodeId": "dock", "sequenceId": 0, "nodePosition": {"x": 1, "y": 2, "mapId": "map"}}],
            "edgeStates": [],
            "zoneIds": ["zone"],
        }
        site_memory.ingest_vda_state(root, message, site="demo", robot="robot", scopes=[])
        graph = site_memory.scene_graph(root, site="demo")
        return _checked(
            bool(graph["layers"].get("dock")), "Validated VDA 5050 state enters the vendor-neutral scene graph"
        )
    if feature == "G27":
        from unimem import safety_case

        identity, digest, adapter = _world(root, seed)
        safety_case.add_claim(root, "stop", hazard="collision", statement="Synthetic stop claim.")
        mined = safety_case.mine(
            root,
            {"failed": True, "episode_id": "failure", "hazard": "collision"},
            claim_id="stop",
            replay=lambda event: {"failed": True},
            dependencies=[{"kind": "world_model", "digest": digest}],
        )
        safety_case.mark_changed(root, kind="world_model", digest=digest)
        queued = safety_case.queue_gate(
            root,
            mined["evidence_id"],
            memory_id=identity,
            model_digest=digest,
            embodiment="demo-arm",
            task="place",
            state={},
            action={"force": 2},
            perturbations=[{"force": 3}],
            adapter=adapter,
        )
        report = safety_case.coverage(root, required_hazards=["collision"])
        return _checked(
            mined["kept"] and queued["status"] == "sim-held" and report["gaps"] == ["collision"],
            "Reproduced safety scenario becomes stale on model change and remains held pending real evidence",
        )
    if feature == "G28":
        from unimem.robotics import shield

        formula = {
            "op": "always",
            "from": 0,
            "to": 1,
            "arg": {"op": "predicate", "signal": "force", "cmp": "<=", "value": 10},
        }
        shield.register(
            root, "force", formula=formula, memory_id="safe", embodiment="demo-arm", description="Synthetic force rule."
        )
        safe = shield.check(
            root,
            "force",
            action={"move": "start"},
            trace=[{"t": 0, "force": 1}, {"t": 1, "force": 2}],
            source_memory_ids=["safe"],
        )
        held = shield.check(
            root, "force", action={"move": "start"}, trace=[{"t": 0, "force": 1}], source_memory_ids=["safe"]
        )
        return _checked(
            safe["action"] == "continue" and held["action"] == "hold",
            "Complete safe trace passes, incomplete temporal evidence holds the action",
        )
    raise ValueError("UniMem pack references an unsupported offline workflow: " + feature)


_CACHE: dict[tuple[tuple[str, ...], int], dict[str, dict[str, Scalar]]] = {}


def execute(features: Sequence[str], *, seed: int) -> dict[str, dict[str, Scalar]]:
    """Exercise each configured feature in a separate temporary governed namespace."""
    fp.count(seed, 0, 2**32 - 1, "demo seed")
    if not isinstance(features, (list, tuple)) or not 1 <= len(features) <= 30:
        raise ValueError("UniMem demo features must be a bounded array")
    if any(not isinstance(feature, str) for feature in features) or len(set(features)) != len(features):
        raise ValueError("UniMem demo feature IDs must be distinct strings")
    cache_key = (tuple(features), seed)
    if cache_key in _CACHE:
        return {k: dict(v) for k, v in _CACHE[cache_key].items()}
    checks = {}
    with offline.without_network(), tempfile.TemporaryDirectory(prefix="unimem-pack-demo-") as directory:
        for feature in features:
            # Fixed G-number routing keeps this an internal allowlist, never an import path.
            if not feature.startswith("G") or not feature[1:].isdigit() or len(feature) > 3:
                raise ValueError("UniMem invalid demo feature ID")
            root = Path(directory) / feature
            root.mkdir()
            checks[feature] = _case(feature, str(root), seed)
    if len(_CACHE) >= 64:
        _CACHE.clear()
    _CACHE[cache_key] = {k: dict(v) for k, v in checks.items()}
    return checks


# The flagship `unimem demo` tour: remember, measure, govern, learn against simulated
# agents with planted effects. Everything but the agent is the real product.
TOUR_MEMORIES = (
    ("verify-owner", "Verify the account owner before any password reset.", 0.20),
    ("retry-429", "On HTTP 429, back off exponentially and retry.", 0.0),
    ("legacy-refund-api", "Issue refunds through the v1 refunds endpoint.", -0.20),
)
TOUR_BASE_SUCCESS = 0.55
TOUR_FLEET_KINDS = (
    ("agent", "agent/coding/fix-bug"),
    ("llm", "llm/chat/support"),
    ("robot", "robot/arm/pick"),
)
TOUR_FLEET_MEMORIES = (
    ("confirm-before-acting", "Confirm the target before acting on it.", {"agent": 0.15, "llm": 0.15, "robot": 0.15}),
    ("move-fast-skip-checks", "Skip verification steps to finish faster.", {"agent": 0.0, "llm": 0.0, "robot": -0.20}),
    ("cite-sources", "Cite the source for every claim.", {"agent": 0.0, "llm": 0.10, "robot": 0.0}),
)


def _tour_cli(*argv: str) -> str:
    from unimem import cli

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    if code:
        raise RuntimeError(f"unimem {' '.join(argv[:2])} failed: {err.getvalue().strip()}")
    return out.getvalue()


def _tour_remember(root: str) -> dict[str, Any]:
    today = datetime.now(timezone.utc).date()
    switch = today - timedelta(days=120)
    _tour_cli(
        "fact",
        "add",
        "Refunds are issued through the v1 refunds endpoint.",
        "--category",
        "tool_rule",
        "--valid-from",
        (today - timedelta(days=540)).isoformat(),
        "--valid-until",
        (switch - timedelta(days=1)).isoformat(),
        "--dest",
        root,
    )
    _tour_cli(
        "fact",
        "add",
        "Refunds are issued through the v2 payments API.",
        "--category",
        "tool_rule",
        "--valid-from",
        switch.isoformat(),
        "--dest",
        root,
    )
    question = "How are refunds issued?"
    as_of = (today - timedelta(days=300)).isoformat()

    def top(*extra: str) -> str:
        raw = _tour_cli("recall", question, "--channel", "facts", "--json", *extra, "--dest", root)
        items = json.loads(raw)["items"]
        return str(items[0]["text"]) if items else ""

    return {"question": question, "today": top(), "as_of": as_of, "then": top("--as-of", as_of)}


def _tour_measure(root: str, occasions: int, seed: int) -> dict[str, Any]:
    from unimem import holdout_io
    from unimem.measure import CausalMemory

    memories = [{"id": key, "text": text} for key, text, _ in TOUR_MEMORIES]
    planted = {key: effect for key, _, effect in TOUR_MEMORIES}
    holdout_io.configure(root, rate=0.5, salt=f"demo-{seed}", note="unimem demo")
    memory = CausalMemory(lambda task, **_: memories, root=root, durable=False)
    rng = random.Random(seed)
    for i in range(occasions):
        occasion = f"ticket-{i:05d}"
        shown = memory.recall("customer support ticket", occasion_id=occasion, context="support")
        p = TOUR_BASE_SUCCESS + sum(planted[m["id"]] for m in shown)
        memory.record_outcome(occasion, succeeded=rng.random() < min(0.98, max(0.02, p)))
    report = json.loads(_tour_cli("experiment", "--json", "--dest", root))
    rows = []
    for key, _, effect in TOUR_MEMORIES:
        found: dict[str, Any] = next((e for e in report["effects"] if e["lesson_slug"] == key), {})
        rows.append(
            {
                "memory": key,
                "planted": effect,
                "verdict": found.get("verdict", "UNDERPOWERED"),
                "effect": found.get("effect"),
                "ci": [found.get("ci_low"), found.get("ci_high")],
                "with": found.get("rate_injected"),
                "without": found.get("rate_withheld"),
            }
        )
    return {"occasions": occasions, "integrity": report["integrity"]["verdict"], "memories": rows}


def _tour_govern(root: str) -> dict[str, Any]:
    from unimem import retrieval_io
    from unimem.measure import CausalMemory

    retrieval_io.configure(root, harm_policy="withdraw", note="unimem demo")
    memories = [{"id": key, "text": text} for key, text, _ in TOUR_MEMORIES]
    memory = CausalMemory(lambda task, **_: memories, root=root, durable=False, check_every=1, graduate=True)
    result = memory.recall_detailed("customer support ticket", occasion_id="ticket-next", record=False)
    settled = set(result.withdrawn) | set(result.graduated)
    return {
        "withdrawn": sorted(result.withdrawn),
        "graduated": sorted(result.graduated),
        "under_test": [key for key, _, _ in TOUR_MEMORIES if key not in settled],
    }


def _tour_learn(root: str, occasions: int, seed: int) -> dict[str, Any]:
    from unimem import causal_gate, holdout_io, rewards
    from unimem.measure import CausalMemory

    sub = os.path.join(root, "fleet")
    _tour_cli("init", "--dest", sub)
    holdout_io.configure(sub, rate=0.5, salt=f"demo-fleet-{seed}", note="unimem demo")
    memories = [{"id": key, "text": text} for key, text, _ in TOUR_FLEET_MEMORIES]
    fleet_truth = {key: effects for key, _, effects in TOUR_FLEET_MEMORIES}
    store = CausalMemory(lambda task, **_: memories, root=sub, durable=False)
    rng = random.Random(seed)
    for i in range(occasions):
        kind, context = TOUR_FLEET_KINDS[i % len(TOUR_FLEET_KINDS)]
        occasion = f"{kind}-{i:05d}"
        shown = store.recall("fleet task", occasion_id=occasion, context=context)
        lift = sum(fleet_truth[m["id"]][kind] for m in shown)
        reward = min(1.0, max(0.0, 0.5 + lift + rng.gauss(0, 0.1)))
        store.record_outcome(occasion, reward=reward)
    problems = rewards.verify(sub)
    model = causal_gate.fit(causal_gate.current_rows(sub))
    rows = []
    for key, _, _ in TOUR_FLEET_MEMORIES:
        effects = {}
        for kind, context in TOUR_FLEET_KINDS:
            p = model.posterior(key, context)
            effects[kind] = None if p is None else {"mean": p.mean, "ci": [p.mean - 1.96 * p.sd, p.mean + 1.96 * p.sd]}
        rows.append({"memory": key, "effects": effects})
    return {
        "occasions": occasions,
        "audit": "SOUND" if not problems else f"{len(problems)} problems",
        "memories": rows,
    }


def _tour_pct(value: float | None) -> str:
    return "   n/a" if value is None else f"{value * 100:+5.0f}%"


def print_tour(result: dict[str, Any]) -> None:
    r, m, g, n = result["remember"], result["measure"], result["govern"], result["learn"]
    print(f"UniMem demo  ·  offline, no LLM, no API key  ·  {result['seconds']:.1f}s\n")
    print("1/4 REMEMBER  facts that know when they were true")
    print(f"    Q: {r['question']}")
    print(f"    today            → {r['today']}")
    print(f"    as of {r['as_of']} → {r['then']}\n")
    print(f"2/4 MEASURE   {m['occasions']:,} simulated support tickets; every memory randomly withheld on half")
    print(f"    integrity audit: {m['integrity']}")
    print(f"    {'memory':<20}{'planted':>9}{'measured':>10}   {'95% interval':<17}verdict")
    for row in m["memories"]:
        low, high = row["ci"]
        interval = "" if low is None else f"[{low * 100:+.0f}%, {high * 100:+.0f}%]"
        planted, measured = _tour_pct(row["planted"]), _tour_pct(row["effect"])
        print(f"    {row['memory']:<20}{planted:>9}{measured:>10}   {interval:<17}{row['verdict']}")
    print()
    print("3/4 GOVERN    the evidence changes what the agent sees next")
    print(f"    withdrawn  (proven harmful, never delivered)  {', '.join(g['withdrawn']) or 'none'}")
    print(f"    graduated  (proven helpful, always delivered) {', '.join(g['graduated']) or 'none'}")
    print(f"    under test (still randomized)                 {', '.join(g['under_test']) or 'none'}\n")
    print(f"4/4 LEARN     {n['occasions']:,} graded tasks from coding agents, LLM apps and robots in one store")
    print("    reward: agent = share of tests passing · llm = evaluator score · robot = task progress")
    print(f"    {'measured (planted)':<24}" + "".join(f"{kind:>14}" for kind, _ in TOUR_FLEET_KINDS))
    fleet_truth = {key: effects for key, _, effects in TOUR_FLEET_MEMORIES}
    for row in n["memories"]:
        cells = []
        for kind, _ in TOUR_FLEET_KINDS:
            cell = row["effects"][kind]
            measured = _tour_pct(None if cell is None else cell["mean"]).strip()
            cells.append(f"{measured} ({fleet_truth[row['memory']][kind] * 100:+.0f}%)")
        print(f"    {row['memory']:<24}" + "".join(f"{cell:>14}" for cell in cells))
    print(f"    reward audit: {n['audit']}")
    print("    One memory can help one kind of client and hurt another; the store estimates each context")
    print("    separately instead of averaging them together.\n")
    print("The agent and its outcomes are simulated with planted effects. The store, randomization,")
    print("integrity audit, causal analysis and withdrawal are the real product.")
    if result.get("store"):
        print(f"Store kept at {result['store']}  ·  try: unimem experiment --dest {result['store']}")
    print("Next: unimem init --dest ./mem   ·   unimem install --target claude-code")


def tour(occasions: int, seed: int, keep: str | None = None) -> dict[str, Any]:
    """Run the tour in a fresh store; ``keep`` names a new directory to retain it."""
    root = keep or tempfile.mkdtemp(prefix="unimem-demo-")
    started = time.monotonic()
    try:
        _tour_cli("init", "--dest", root)
        result: dict[str, Any] = {
            "remember": _tour_remember(root),
            "measure": _tour_measure(root, occasions, seed),
            "govern": _tour_govern(root),
            "learn": _tour_learn(root, occasions, seed),
        }
    finally:
        if not keep:
            shutil.rmtree(root, ignore_errors=True)
    result["seconds"] = round(time.monotonic() - started, 2)
    if keep:
        result["store"] = os.path.abspath(root)
    return result
