"""UniMem foundry calibration, causal delivery, admission and portable evidence."""
from __future__ import annotations

import json
import os
import random
import types
from datetime import datetime, timedelta, timezone

import pytest

from commontrace import (
    aipw,
    allocation,
    conformal_gate,
    experiment,
    frontmatter,
    holdout_io,
    memory_authority,
    promotion,
    skill_foundry,
    skill_io,
    trace_io,
    unit_trials,
)
from commontrace import frontier_protocol as fp


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        moment = datetime(2026, 10, 16, tzinfo=timezone.utc)

        def tick(self, seconds=10):
            self.moment += timedelta(seconds=seconds)

    clock = Clock()

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.moment

    frozen = types.SimpleNamespace(datetime=Frozen, timedelta=timedelta, timezone=timezone)
    monkeypatch.setattr(allocation, "datetime", frozen)
    monkeypatch.setattr(holdout_io, "datetime", frozen)
    monkeypatch.setattr(fp, "now", lambda: clock.moment.isoformat())
    monkeypatch.setattr(os, "fsync", lambda fd: None)
    return clock


def source(root, *, success=True, context=(), text="Check the release signature."):
    path = trace_io.write_new(root, title="Measured release", context="Release review", solution=text,
                             tags=["release"], outcome={"resolved": success}, extra={"scopes": list(context)})
    return trace_io.read(path)[0]["id"]


def tasks(identity, token="signature"):
    return [{"trace_id": identity, "instruction": "Prevent the measured signature-check failure.",
             "verifier": {"contains": token}, "passing": "Check the " + token, "failing": "Skip verification."}]


def prepare(root, clock, *, name="release-check", context=(), enroll=True):
    holdout_io.configure(root, rate=.5, salt="skill-pilot")
    good, bad = source(root, context=context), source(root, success=False, context=context)
    candidate = skill_io.induce(root, name, "Verify release signatures before deployment.", [good], context=context)
    skill_io.screen(root, candidate["id"], tasks(bad), context=context)
    # Freeze the harm calibration from actual ungated, completed pilot assignments.
    for i in range(80):
        holdout_io.assign_and_log(root, ["pilot"], occasion_id="pilot-"+str(i), rate=.5, salt="skill-pilot",
                                 relevance={"pilot": 1}, scorer="unimem-skill")
        holdout_io.record_outcome(root, "pilot-"+str(i), True)
    conformal_gate.configure(root, alpha=.1, propensity_floor=.5, scorer="unimem-skill")
    clock.tick()
    if enroll:
        skill_io.trial(root, candidate["id"], context=context, training_size=10, minimum_support=2)
    clock.tick()
    return candidate


def test_seeded_delivery_aipw_bias_and_focused_bundle_size_factor():
    rng = random.Random(11)
    estimates, bundles = [], []
    candidate = {"id": "candidate"}
    for _ in range(80):
        observations = []
        bundle_scores = []
        for i in range(300):
            rate = .2 if i < 150 else .8
            arm = rng.random() >= rate
            actual = bool(skill_foundry.exposed(candidate, arm))
            baseline = .1 if i < 150 else .5
            outcome = rng.random() < baseline + .3*actual
            observations.append(aipw.Observation(arm, outcome, rate))
            large = rng.random() < .5
            # The bundle factor is independently randomized before inner exposure.
            bundle_outcome = rng.random() < .4 + .2*large
            bundle_scores.append((2 if large else -2)*bundle_outcome)
        estimates.append(sum(aipw.aipw_scores(observations))/len(observations))
        bundles.append(sum(bundle_scores)/len(bundle_scores))
    assert sum(estimates)/len(estimates) == pytest.approx(.3, abs=.025)
    assert sum(bundles)/len(bundles) == pytest.approx(.2, abs=.025)


def test_seeded_skill_harm_and_useless_promotion():
    rng = random.Random(19)
    loss = false_promotions = 0
    for repeat in range(40):
        calibration = [conformal_gate.Calibration(.1 if (unsafe := rng.random() < .2) else .9,
                       rng.random() < .5, unsafe, .5) for _ in range(250)]
        gate = conformal_gate.calibrate(calibration, alpha=.05, propensity_floor=.5)
        fresh = [rng.random() < .2 for _ in range(100)]
        loss += sum(unsafe and conformal_gate.admit(gate, .1 if unsafe else .9) for unsafe in fresh)
        rows = [{"occasion_id": str(i), "injected": rng.random() < .5, "outcome": int(rng.random() < .5),
                 "rate": .5} for i in range(220)]
        false_promotions += promotion.evaluate(rows, {"default": 1}, {"default": 0},
                               training_size=20, minimum_support=2, propensity_floor=.5)["promote"]
    assert loss/4000 <= .05
    assert false_promotions <= 4


def test_template_screen_and_optional_program_sandbox():
    value = skill_foundry.candidate([{"solution_text": "Check signature."}],
                                   name="release-check", description="Verify.")
    assert skill_foundry.prescreen(value, tasks("failure"))["passed"]
    assert not skill_foundry.prescreen(value, tasks("failure", "hash"))["passed"]
    program = {**value, "format": "program", "body": "return verified_signature"}
    with pytest.raises(ValueError, match="sandbox"):
        skill_foundry.prescreen(program, tasks("failure"))
    assert skill_foundry.prescreen(program, tasks("failure"), sandbox=lambda program, task: "signature")["passed"]
    for name in ("-name", "Name", "a--b", "a-", "x"*65):
        with pytest.raises(ValueError):
            skill_foundry.metadata(name, "description")
    with pytest.raises(ValueError, match="fixtures"):
        skill_foundry.prescreen(value, [{**tasks("failure")[0], "failing": "signature"}])


def test_schedule_delivery_integrity_and_private_catalog(tmp_path, clock):
    root = str(tmp_path)
    value = prepare(root, clock, context=["team:a"])
    identity = value["id"]
    assert not skill_io.catalog(root, "private", context=["team:b"])["skills"]
    for i in range(20):
        occasion = "episode-" + str(i)
        catalog = skill_io.catalog(root, occasion, context=["team:a"])
        expected = not experiment.is_held_out(identity, occasion, .5, "skill-pilot")
        assert bool(catalog["skills"]) is expected
        holdout_io.record_outcome(root, occasion, expected)
        clock.tick(1)
    rows, corrupt = holdout_io.read_log(root)
    own = [r for r in rows if r.lesson == identity]
    assert len(own) == 20 and not corrupt and all(r.schedule for r in own)
    assert unit_trials.report(root, identity)["n"] == 10
    assert allocation.verify(root) == []
    # Transport retries must not become duplicate canonical evidence.
    skill_io.catalog(root, "episode-19", context=["team:a"])
    assert len([r for r in holdout_io.read_log(root)[0] if r.lesson == identity]) == 20
    with pytest.raises(ValueError, match="promotion margin"):
        skill_io.publish(root, identity, context=["team:a"])
    memory_authority.record_forgetting(root, next(iter(value["source_digests"])), forgotten=True)
    assert not skill_io.catalog(root, "after-forgetting", context=["team:a"])["skills"]


def test_portable_folder_certificate_and_revocation(tmp_path, clock):
    from commontrace import skills

    root = str(tmp_path)
    value = prepare(root, clock)
    for i in range(420):
        catalog = skill_io.catalog(root, "live-"+str(i))
        holdout_io.record_outcome(root, "live-"+str(i), bool(catalog["skills"]))
        clock.tick(1)
    exported = skill_io.publish(root, value["id"])
    directory = tmp_path / "skills" / value["name"]
    fm, body = frontmatter.read(str(directory / "SKILL.md"))
    assert set(fm) == {"name", "description"} and "EVIDENCE.json" in body
    assert fp.verify(root, exported["signed"])
    assert skill_io.evidence(root, value["id"])["signed"] == exported["signed"]
    cached = next(s for s in skills.discover(root, include_bundled=False) if s.name == value["name"])
    assert skills.load_body(cached)
    promotion.rollback(root, exported["signed"]["record"]["certificate"], "test rollback")
    assert not any(s.name == value["name"] for s in skills.discover(root, include_bundled=False))
    with pytest.raises(PermissionError):
        skill_io.evidence(root, value["id"])
    assert skills.load_body(cached) == ""
    from commontrace import nextwave_store as store

    removed = store.erase(root, list(value["source_digests"]))
    assert any(r["kind"] == "skill-exports" for r in removed)
    assert not (directory / "SKILL.md").exists() and not (directory / "EVIDENCE.json").exists()
    assert not skills.discover(root, include_bundled=False)


def test_unmeasured_or_untrusted_sources_never_induce(tmp_path):
    root = str(tmp_path)
    with pytest.raises(PermissionError):
        skill_io.induce(root, "skill", "description", ["missing"])
    with memory_authority.writer("untrusted-agent", "agent"):
        identity = source(root)
    with pytest.raises(PermissionError):
        skill_io.induce(root, "skill", "description", [identity])


def test_cli_and_mcp_surfaces(tmp_path, clock):
    pytest.importorskip("mcp")
    from commontrace.mcp_server import build_server
    from tests.test_mcp_server import call, cli

    root = str(tmp_path)
    identity = source(root)
    result = cli("skill", "induce", json.dumps({"name": "check", "description": "Verify signatures.",
                 "trace_ids": [identity]}), "--dest", root)
    assert result.returncode == 0, result.stderr
    server = build_server(root)
    assert call(server, "skill_catalog", occasion="not-enrolled")["skills"] == []
    assert not call(server, "skill_evidence", identity="skill:" + "a"*64)["ok"]


def test_bundle_size_is_chosen_before_individual_eligibility(tmp_path, clock):
    root = str(tmp_path)
    first = prepare(root, clock, name="first", enroll=False)
    failure = source(root, success=False)
    members = [first["id"]]
    for name in ("second", "third", "fourth"):
        candidate = skill_io.induce(root, name, "Check signatures.", list(first["source_digests"]))
        skill_io.screen(root, candidate["id"], tasks(failure))
        members.append(candidate["id"])
    config = skill_io.trial(root, first["id"], bundle_ids=members[1:], training_size=10, minimum_support=2)
    with pytest.raises(ValueError, match="effective"):
        skill_io.catalog(root, "too-soon")
    clock.tick()
    for i in range(12):
        occasion = "bundle-" + str(i)
        skill_io.catalog(root, occasion)
        large = not experiment.is_held_out(config["bundle_unit"], occasion, .5, "skill-pilot")
        assigned = {r.lesson for r in holdout_io.read_log(root)[0] if r.occasion_id == occasion}
        assert config["bundle_unit"] in assigned
        assert set(sorted(members)[:3]) <= assigned
        assert (sorted(members)[3] in assigned) is large
        holdout_io.record_outcome(root, occasion, large)
        clock.tick(1)
    from commontrace import nextwave_store as store

    excluded = store.events(root, "unit-eligibility")
    assert excluded and all(row["status"] == "ineligible" for row in excluded)


def test_artifact_erasure_removes_transitive_content(tmp_path):
    from commontrace import nextwave_store as store

    root = str(tmp_path)
    identity = source(root)
    candidate = skill_io.induce(root, "release", "Check signature.", [identity])
    store.put(root, "descendant", "derived", {"text": "signature"}, sources=[candidate["id"]])
    removed = store.erase(root, [identity])
    assert {candidate["id"], "derived"} <= {r["id"] for r in removed}
    with pytest.raises(ValueError, match="unavailable"):
        store.get(root, skill_io.KIND, candidate["id"])


def test_post_promotion_decay_retires_and_cannot_be_republished(tmp_path, clock):
    root = str(tmp_path)
    value = prepare(root, clock)
    for i in range(260):
        occasion = "good-" + str(i)
        catalog = skill_io.catalog(root, occasion)
        holdout_io.record_outcome(root, occasion, bool(catalog["skills"]))
        clock.tick(1)
    skill_io.publish(root, value["id"])
    signal = {"retire": False}
    for i in range(150):
        occasion = "bad-" + str(i)
        catalog = skill_io.catalog(root, occasion)
        holdout_io.record_outcome(root, occasion, not bool(catalog["skills"]))
        clock.tick(1)
        signal = skill_io.retirement_signal(root, value["id"])
        if signal["retire"]:
            break
    assert signal["retire"] and signal["decay_e_value"] >= signal["alarm_threshold"] == 40
    with pytest.raises(PermissionError, match="republished"):
        skill_io.publish(root, value["id"])
    assert skill_io.retire(root, value["id"])["state"] == "retired"
    assert not skill_io.catalog(root, "after-retirement")["skills"]
    skill_io.induce(root, value["name"], value["description"], list(value["source_digests"]))
    with pytest.raises(PermissionError, match="restart"):
        skill_io.trial(root, value["id"], training_size=10, minimum_support=2)
    assert not skill_io.catalog(root, "after-reinduction")["skills"]


def test_rendered_body_digest_survives_frontmatter_newline_normalization(tmp_path):
    for i, body in enumerate(("\n\n# Verify\nCheck signatures.", "\r\n# Verify\r\nCheck signatures.\r\n")):
        value = {"name": "release-check", "format": "prose", "body": body}
        rendered = skill_foundry.document(value)
        path = str(tmp_path / (str(i) + ".md"))
        frontmatter.write(path, {"name": "release-check", "description": "Verify release signatures."}, rendered)
        assert fp.digest(frontmatter.read(path)[1]) == fp.digest(rendered)
