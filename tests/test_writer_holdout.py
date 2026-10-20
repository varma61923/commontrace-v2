"""UniMem writer masking is a parent intervention, not memory non-delivery."""
from __future__ import annotations

import os
import random
import types
from datetime import datetime, timedelta, timezone

import pytest

from commontrace import aipw, allocation, eprocess, hierarchical, holdout_io, incident, memory_authority, safety
from commontrace import frontier_protocol as fp
from commontrace import nextwave_store as store
from commontrace import writer_holdout as writers


@pytest.fixture
def clock(monkeypatch):
    moment = datetime(2026, 10, 16, tzinfo=timezone.utc)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment

    frozen = types.SimpleNamespace(datetime=Frozen, timedelta=timedelta, timezone=timezone)
    monkeypatch.setattr(allocation, "datetime", frozen)
    monkeypatch.setattr(holdout_io, "datetime", frozen)
    monkeypatch.setattr(fp, "now", lambda: moment.isoformat())
    monkeypatch.setattr(os, "fsync", lambda _: None)

    def tick():
        nonlocal moment
        moment += timedelta(seconds=10)
    return tick


def test_nested_writer_mask_has_no_fake_memory_control_rows(tmp_path, clock):
    root = str(tmp_path)
    with memory_authority.writer("agent-a", "agent"):
        fact, _ = hierarchical.append_facts(root, [{"statement": "Check signatures on every deployment."}])[0]
        hazard, _ = hierarchical.append_facts(root, [{"statement": "Stop when a person enters the robot cell."}])[0]
    receipt = safety.review(root, hazard.id, reviewer="operator", reason="Safety interlock instruction")
    safety.mark(root, hazard.id, "human-proximity", receipt)
    holdout_io.configure(root, rate=.5, salt="writers")
    writers.register(root, "reviewers", members=["agent-a"], training_size=10, minimum_support=1)
    clock()
    masked = 0
    for i in range(24):
        occasion = str(i)
        with writers.reader(root, occasion) as parent:
            inner = holdout_io.assign_and_log(root, [fact.id, hazard.id], occasion_id=occasion, rate=.5, salt="writers")
            from commontrace import search_recipes
            visible = search_recipes.admit(root, [fact.id, hazard.id])
            assert hazard.id in visible and hazard.id not in inner
            if not parent["writers"]["reviewers"]:
                masked += 1
                assert fact.id in inner and fact.id not in visible
        holdout_io.record_outcome(root, occasion, True)
    rows, corrupt = holdout_io.read_log(root)
    assert not corrupt and masked > 0
    memory_rows = [r for r in rows if r.lesson == fact.id]
    assert len(memory_rows) == 24 - masked
    assert len([r for r in rows if r.lesson == "writer:reviewers"]) == 24
    assert len(store.events(root, "writer-eligibility")) == masked
    assert all(r.lesson != hazard.id for r in rows)


def test_writer_effect_coverage_and_nested_bias_mutation():
    rng = random.Random(412)
    coverage, correct, mutated = 0, [], []
    for repeat in range(100):
        parent, inner, incorrect = [], [], []
        for i in range(400):
            writer = rng.random() < .5
            memory = rng.random() < .5
            y = rng.random() < .1 + .3*writer + .5*(writer and memory)
            parent.append(aipw.Observation(writer, y, .5))
            if writer:
                inner.append(aipw.Observation(memory, y, .5))
            # Deliberate mutation: writer-masked memories falsely counted as
            # independent memory control observations at propensity one-half.
            incorrect.append(aipw.Observation(writer and memory, y, .5))
        scores = aipw.aipw_scores(parent)
        _, low, high = eprocess.interval(scores, bound=2, alpha=.05)
        coverage += low <= .55 <= high
        correct.append(sum(aipw.aipw_scores(inner))/len(inner))
        mutated.append(sum(aipw.aipw_scores(incorrect))/len(incorrect))
    assert coverage >= 93
    assert sum(correct)/len(correct) == pytest.approx(.5, abs=.025)
    assert abs(sum(mutated)/len(mutated)-.5) > .1


def test_reliability_recovers_good_and_bad_writer(tmp_path, clock):
    root = str(tmp_path)
    holdout_io.configure(root, rate=.5, salt="ranking")
    for writer in ("good", "bad"):
        writers.register(root, writer, training_size=20, minimum_support=2)
    clock()
    for i in range(100):
        with writers.reader(root, "good-"+str(i), identities=["good"]) as parent:
            holdout_io.record_outcome(root, "good-"+str(i), parent["writers"]["good"])
        with writers.reader(root, "bad-"+str(i), identities=["bad"]) as parent:
            holdout_io.record_outcome(root, "bad-"+str(i), not parent["writers"]["bad"])
    assert writers.reliability(root, "good")["score"] > .5
    assert writers.reliability(root, "bad")["score"] < -.5
    with memory_authority.writer("good", "agent"):
        good, _ = hierarchical.append_facts(root, [{"statement": "Signed packages need verification."}])[0]
    with memory_authority.writer("bad", "agent"):
        bad, _ = hierarchical.append_facts(root, [{"statement": "Unverified packages are acceptable."}])[0]
    assert writers.rank(root, [bad.id, good.id])[0]["id"] == good.id
    with writers.reader(root, "outer"):
        with pytest.raises(ValueError, match="nest"):
            with writers.reader(root, "inner"):
                pass
        with pytest.raises(ValueError, match="match"):
            writers.partition(root, [good.id], "wrong-occasion")


def test_mast_rules_validate_taxonomy_and_incident(tmp_path, clock):
    root = str(tmp_path)
    incident.freeze(root, "incident-1", fp.now())
    tags = writers.tag_mast(root, "incident-1", "Agent ignored input and stopped early after weak verification.")
    assert {t["code"] for t in tags["tags"]} == {"2.5", "3.1", "3.2"}
    assert len(writers.MAST) == 14
    with pytest.raises(ValueError, match="taxonomy"):
        writers.tag_mast(root, "incident-1", "event", tagger=lambda _: ["unknown"])
    with memory_authority.writer("agent", "agent"):
        with pytest.raises(PermissionError):
            writers.register(root, "forged-role")
        with pytest.raises(PermissionError):
            writers.tag_mast(root, "incident-1", "event")


def test_cli_mask_delivers_nested_arm_and_route_prior(tmp_path, clock, capsys):
    import json

    from commontrace import cli

    root = str(tmp_path)
    with memory_authority.writer("reviewer", "agent"):
        fact, _ = hierarchical.append_facts(root, [{"statement": "Check package signature evidence."}])[0]
    holdout_io.configure(root, rate=.5, salt="cli-writers")
    assert cli.main(["team", "register-writer", json.dumps({"writer": "reviewer"}), "--dest", root]) == 0
    capsys.readouterr()
    clock()
    body = json.dumps({"occasion": "cli-occasion", "memories": [fact.id]})
    assert cli.main(["team", "mask", body, "--dest", root]) == 0
    result = json.loads(capsys.readouterr().out)
    if not result["writers"]["reviewer"]:
        assert result["deliver"] == [] and result["ineligible"] == [fact.id]
    assert writers.route(root, ["reviewer"])["classification"] == "estimate"
    assert writers.retrieve(root, "package signature")


def test_mcp_writer_estimate_matches_canonical_report(tmp_path, clock):
    import asyncio
    import json

    pytest.importorskip("mcp")
    from commontrace import mcp_server

    root = str(tmp_path)
    holdout_io.configure(root, rate=.5, salt="mcp-writers")
    writers.register(root, "reviewer")
    server = mcp_server.build_server(root)
    result = asyncio.run(server.call_tool("writer_reliability", {"writer": "reviewer"}))
    payload = json.loads(result.content[0].text)
    assert payload["ok"] and payload["classification"] == "estimate"
    assert payload["score"] == writers.reliability(root, "reviewer")["score"]
