import json
import os
import random

import pytest

from commontrace import experiment, fleet, gateway, holdout_io, integrity
from commontrace.cli import main
from commontrace.commands import experiment_cmd


@pytest.fixture(autouse=True)
def _no_fsync(monkeypatch):
    monkeypatch.setattr(os, "fsync", lambda fd: None)


def _lead(tmp_path):
    root = str(tmp_path / "lead")
    holdout_io.configure(root, rate=0.5, detect=0.1, salt="fleet-salt")
    gateway.save_config(root, gateway.GatewayConfig(env="real", protected_prefixes=("safety/",)))
    return root


def _robot(tmp_path, name, lead, n, *, effect=0.2, seed=0, prefix=None, durable=True):
    root = str(tmp_path / name)
    fleet.adopt(lead, root)
    config = holdout_io.load_config(root)
    rng = random.Random(f"{name}:{seed}")
    for i in range(n):
        occasion = f"{prefix or name}/ep-{i}"
        withheld = holdout_io.assign_and_log(root, ["grip-tuning"], occasion_id=occasion, rate=config.rate,
                                             salt=config.salt, revisions={"grip-tuning": "r1"})
        p = 0.5 + (effect if "grip-tuning" not in withheld else 0)
        holdout_io.record_outcome(root, occasion, rng.random() < p)
    return root


def _effects(root):
    rows, _s, _o = experiment_cmd.scope_to_current_salt(root, experiment_cmd._load(root)[0])
    return rows, {e.lesson_slug: e for e in experiment.analyze(experiment_cmd._observations(rows))}


def test_adopt_gives_a_robot_the_same_randomization_and_policy(tmp_path):
    lead = _lead(tmp_path)
    done = fleet.adopt(lead, str(tmp_path / "r1"))
    assert done == {"salt": "fleet-salt", "rate": 0.5, "env": "real", "protected_prefixes": ["safety/"]}
    config = holdout_io.load_config(str(tmp_path / "r1"))
    assert (config.salt, config.rate, config.started_at) == ("fleet-salt", 0.5, holdout_io.load_config(lead).started_at)
    assert gateway.load_config(str(tmp_path / "r1")).env == "real"


def test_adopt_refuses_a_lead_with_no_experiment_and_a_robot_that_already_has_data(tmp_path):
    with pytest.raises(fleet.FleetError, match="no experiment started"):
        fleet.adopt(str(tmp_path / "nothing"), str(tmp_path / "r"))
    lead = _lead(tmp_path)
    robot = _robot(tmp_path, "r1", lead, 5)
    other = str(tmp_path / "other-lead")
    holdout_io.configure(other, rate=0.3, salt="different")
    with pytest.raises(fleet.FleetError, match="already holds assignments"):
        fleet.adopt(other, robot)
    assert holdout_io.load_config(robot).salt == "fleet-salt"


def test_merging_robots_gives_the_pooled_sample_and_leaves_the_sources_alone(tmp_path):
    lead = _lead(tmp_path)
    robots = [_robot(tmp_path, f"r{n}", lead, 200) for n in range(3)]
    before = [open(holdout_io.holdout_log_path(r), "rb").read() for r in robots]
    dest = str(tmp_path / "merged")
    totals = fleet.merge(robots, dest)
    assert totals["assignments"] == 600 and totals["outcomes"] == 600 and totals["duplicates"] == 0
    rows, effects = _effects(dest)
    assert len({r.occasion_id for r in rows}) == 600
    assert effects["grip-tuning"].n_injected + effects["grip-tuning"].n_withheld == 600
    assert effects["grip-tuning"].verdict == experiment.VERDICT_HELPS
    assert [open(holdout_io.holdout_log_path(r), "rb").read() for r in robots] == before
    assert integrity.audit(rows).verdict == integrity.VERDICT_SOUND
    assert holdout_io.load_config(dest).salt == "fleet-salt" and gateway.load_config(dest).env == "real"


def test_a_pooled_sample_decides_what_no_single_robot_could(tmp_path):
    lead = _lead(tmp_path)
    robots = [_robot(tmp_path, f"r{n}", lead, 60, effect=0.25) for n in range(8)]
    singles = [_effects(r)[1]["grip-tuning"].verdict for r in robots]
    assert singles.count(experiment.VERDICT_HELPS) < len(robots)
    dest = str(tmp_path / "all")
    fleet.merge(robots, dest)
    assert _effects(dest)[1]["grip-tuning"].verdict == experiment.VERDICT_HELPS


def test_different_randomizations_are_refused_not_pooled(tmp_path):
    lead = _lead(tmp_path)
    good = _robot(tmp_path, "good", lead, 20)
    rogue = str(tmp_path / "rogue")
    holdout_io.configure(rogue, rate=0.5, salt="another-salt")
    report = fleet.check([good, rogue])
    assert not report.mergeable and "different randomization" in report.problems[0]
    with pytest.raises(fleet.FleetError, match="cannot merge"):
        fleet.merge([good, rogue], str(tmp_path / "out"))
    assert not os.path.exists(holdout_io.holdout_log_path(str(tmp_path / "out")))


def test_different_rates_and_environments_are_refused(tmp_path):
    lead = _lead(tmp_path)
    a = _robot(tmp_path, "a", lead, 10)
    b = _robot(tmp_path, "b", lead, 10)
    holdout_io.configure(b, rate=0.2, salt="fleet-salt")
    assert any("held out" in p for p in fleet.check([a, b]).problems)
    c = _robot(tmp_path, "c", lead, 10)
    gateway.save_config(c, gateway.GatewayConfig(env="sim"))
    problems = fleet.check([a, c]).problems
    assert any("never pooled" in p for p in problems)


def test_a_store_with_no_experiment_started_cannot_be_pooled(tmp_path):
    lead = _lead(tmp_path)
    a = _robot(tmp_path, "a", lead, 5)
    assert any("no experiment started" in p for p in fleet.check([a, str(tmp_path / "empty")]).problems)


def test_a_robot_synced_twice_collapses_to_one_but_a_real_conflict_is_named(tmp_path):
    lead = _lead(tmp_path)
    a = _robot(tmp_path, "a", lead, 50)
    copy = str(tmp_path / "a-copy")
    os.makedirs(os.path.dirname(holdout_io.config_path(copy)), exist_ok=True)
    for fn in (holdout_io.config_path, holdout_io.holdout_log_path, holdout_io.outcomes_log_path,
               gateway.config_path):
        with open(fn(a), "rb") as src, open(fn(copy), "wb") as dst:
            dst.write(src.read())
    report = fleet.check([a, copy])
    assert report.mergeable and report.duplicates == 100
    dest = str(tmp_path / "m")
    assert fleet.merge([a, copy], dest)["assignments"] == 50

    clash = _robot(tmp_path, "clash", lead, 50, prefix="a", seed=99, effect=-0.3)
    report = fleet.check([a, clash])
    assert report.conflicts and not report.mergeable
    assert any("ep-" in c and ("opposite arms" in c or "succeeded" in c or "failed" in c) for c in report.conflicts)


def test_namespaced_occasion_ids_never_collide_across_robots(tmp_path):
    lead = _lead(tmp_path)
    a = _robot(tmp_path, "a", lead, 80)
    b = _robot(tmp_path, "b", lead, 80)
    assert fleet.check([a, b]).mergeable


def test_unreadable_lines_are_counted_and_skipped(tmp_path):
    lead = _lead(tmp_path)
    a = _robot(tmp_path, "a", lead, 10)
    b = _robot(tmp_path, "b", lead, 10)
    with open(holdout_io.holdout_log_path(b), "a", encoding="utf-8") as fh:
        fh.write("{torn line\n[1,2]\n")
    report = fleet.check([a, b])
    assert report.mergeable and sum(s.corrupt for s in report.stores) == 2
    assert fleet.merge([a, b], str(tmp_path / "m"), report=report)["assignments"] == 20


def test_merge_will_not_write_into_a_store_that_already_has_data(tmp_path):
    lead = _lead(tmp_path)
    a, b = _robot(tmp_path, "a", lead, 5), _robot(tmp_path, "b", lead, 5)
    with pytest.raises(fleet.FleetError, match="empty store"):
        fleet.merge([a, b], a)
    with pytest.raises(fleet.FleetError, match="at least two"):
        fleet.check([a])


def test_events_and_the_registered_proof_travel_with_the_merge(tmp_path):
    lead = _lead(tmp_path)
    a = _robot(tmp_path, "a", lead, 5)
    b = _robot(tmp_path, "b", lead, 5)
    for root, who in ((a, "robot-a"), (b, "robot-b")):
        with open(os.path.join(root, "memory", gateway.EVENTS_NAME), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": "2026-10-01T00:00:00.000+00:00", "kind": "recall", "agent_id": who}) + "\n")
    open(os.path.join(a, "memory", "proof.json"), "w").write(json.dumps({"schema": 1, "label": "fleet"}))
    dest = str(tmp_path / "m")
    assert fleet.merge([a, b], dest)["events"] == 2
    g = gateway.Gateway(dest, token="t" * 30)
    agents = {x["agent_id"] for x in json.loads(g.handle("GET", "/v1/agents", {"Authorization": "Bearer " + "t" * 30}).body)["agents"]}
    assert agents == {"robot-a", "robot-b"}
    assert json.load(open(os.path.join(dest, "memory", "proof.json")))["label"] == "fleet"


def test_the_cli_runs_adopt_check_merge(tmp_path, capsys):
    lead = _lead(tmp_path)
    a = _robot(tmp_path, "a", lead, 30)
    b = str(tmp_path / "b")
    assert main(["fleet", "adopt", lead, "--dest", b]) == 0
    holdout_io.configure(str(tmp_path / "c"), rate=0.5, salt="other")
    assert main(["fleet", "check", a, str(tmp_path / "c")]) == 1
    assert "CANNOT POOL" in capsys.readouterr().out
    assert main(["fleet", "merge", a, str(tmp_path / "c"), "--dest", str(tmp_path / "x")]) == 1
    capsys.readouterr()
    assert main(["fleet", "merge", a, b, "--dest", str(tmp_path / "ok")]) == 0
    assert "merged 2 stores" in capsys.readouterr().out
    assert main(["fleet", "check", a]) == 2
    assert main(["fleet", "adopt", str(tmp_path / "none"), "--dest", str(tmp_path / "z")]) == 2
