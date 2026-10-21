import pytest

from unimem import safety_case


def test_dependency_change_stales_only_affected_evidence_and_queues_revalidation(tmp_path):
    root = str(tmp_path)
    safety_case.add_claim(root, "collision", hazard="collision", statement="The robot stops before a person.")
    a, b = "a" * 64, "b" * 64
    safety_case.add_evidence(root, "sim_run", claim_id="collision", source_type="trial", source_id="run-1",
                             source_digest=a, dependencies=[{"kind": "model", "digest": a}])
    safety_case.add_evidence(root, "sensor_cert", claim_id="collision", source_type="certificate", source_id="cert-1",
                             source_digest=b, dependencies=[{"kind": "software", "digest": b}])
    changed = safety_case.mark_changed(root, kind="model", digest=a)
    assert [row["evidence_id"] for row in changed] == ["sim_run"]
    report = safety_case.coverage(root, required_hazards=["collision"])
    assert report["hazards"][0]["stale"] == ["sim_run"]
    assert report["gaps"] == ["collision"]


def test_scenario_mining_keeps_only_reproducible_failures(tmp_path):
    root = str(tmp_path)
    safety_case.add_claim(root, "stop_claim", hazard="person_crossing", statement="The robot stops.")
    event = {"failed": True, "episode_id": "episode-1", "hazard": "person crossing"}
    digest = "c" * 64
    kept = safety_case.mine(root, event, claim_id="stop_claim", replay=lambda _: {"failed": True, "trace": digest},
                            dependencies=[{"kind": "software", "digest": digest}])
    assert kept["kept"] and kept["reproductions"] == 2
    assert safety_case.export_scenario(root, kept["scenario_id"])["validation"]["valid"]
    count = len(safety_case.fp.journal(root, safety_case.JOURNAL))
    result = safety_case.mine(root, event, claim_id="stop_claim", replay=lambda _: {"failed": False}, dependencies=[])
    assert not result["kept"]
    assert len(safety_case.fp.journal(root, safety_case.JOURNAL)) == count


def test_claim_and_evidence_are_linked_and_invalid_scenario_rejected(tmp_path):
    root = str(tmp_path)
    with pytest.raises(KeyError):
        safety_case.add_argument(root, "orphan", claim_id="missing", statement="x")
    with pytest.raises(ValueError):
        safety_case.validate_scenario("scenario bad:\n    sut: vehicle")


def test_evidence_for_one_claim_cannot_hide_another_claims_gap(tmp_path):
    root = str(tmp_path)
    for name in ("stop", "sense"):
        safety_case.add_claim(root, name, hazard="collision", statement=name)
    for number in range(2):
        safety_case.add_evidence(root, f"evidence_{number}", claim_id="stop", source_type="trial",
            source_id=f"run-{number}", source_digest=str(number) * 64, dependencies=[])
    report = safety_case.coverage(root, required_hazards=["collision"])
    assert report["gaps"] == ["collision"]
    assert report["hazards"][0]["coverage"] == .5
    assert report["hazards"][0]["missing_claims"] == ["sense"]


def test_duplicate_evidence_sources_do_not_inflate_coverage(tmp_path):
    root = str(tmp_path)
    safety_case.add_claim(root, "stop", hazard="collision", statement="stop", expected_evidence=2)
    for number in range(2):
        safety_case.add_evidence(root, f"evidence_{number}", claim_id="stop", source_type="trial",
            source_id=f"alias-{number}", source_digest="a" * 64, dependencies=[])
    assert safety_case.coverage(root, required_hazards=["collision"])["hazards"][0]["coverage"] == .5


def test_scenario_subset_rejects_unparsed_lines_and_duplicate_actors():
    source = safety_case.scenario_template("crossing", "collision")
    assert safety_case.validate_scenario(source)["valid"]
    for changed in (source + "invalid syntax\n", source.replace("sut.drive()", "sut.missing()"),
                    source.replace("obstacle.drive()", "someone_else.drive()")):
        with pytest.raises(ValueError):
            safety_case.validate_scenario(changed)
    with pytest.raises(ValueError):
        safety_case.scenario_template("crossing", "collision", actor="sut")


def test_replay_aliasing_cannot_make_different_results_look_reproducible(tmp_path):
    root = str(tmp_path)
    safety_case.add_claim(root, "stop", hazard="collision", statement="stop")
    shared = {"failed": True, "step": 0}

    def replay(event):
        shared["step"] += 1
        return shared

    result = safety_case.mine(root, {"failed": True, "episode_id": "episode"}, claim_id="stop",
                              replay=replay, dependencies=[])
    assert not result["kept"]


def test_repeated_mining_is_idempotent_and_does_not_mutate_the_event(tmp_path):
    root = str(tmp_path)
    safety_case.add_claim(root, "stop", hazard="collision", statement="stop")
    event = {"failed": True, "episode_id": "episode", "hazard": "collision"}

    def replay(value):
        value["hazard"] = "modified by adapter"
        return {"failed": True}

    first = safety_case.mine(root, event, claim_id="stop", replay=replay, dependencies=[])
    count = len(safety_case.fp.journal(root, safety_case.JOURNAL))
    assert safety_case.mine(root, event, claim_id="stop", replay=replay, dependencies=[]) == first
    assert event["hazard"] == "collision"
    assert len(safety_case.fp.journal(root, safety_case.JOURNAL)) == count


def test_node_ids_are_unique_across_claims_arguments_and_evidence(tmp_path):
    root = str(tmp_path)
    safety_case.add_claim(root, "stop", hazard="collision", statement="stop")
    with pytest.raises(ValueError, match="unique"):
        safety_case.add_argument(root, "stop", claim_id="stop", statement="duplicate")


def test_completed_revalidation_cannot_replay_a_historical_pending_event(tmp_path, monkeypatch):
    from unimem import world_model_trust

    root = str(tmp_path)
    safety_case.add_claim(root, "stop", hazard="collision", statement="stop")
    safety_case.add_evidence(root, "evidence", claim_id="stop", source_type="trial", source_id="run",
                            source_digest="a" * 64, dependencies=[{"kind": "model", "digest": "a" * 64}])
    safety_case.mark_changed(root, kind="model", digest="a" * 64)
    calls = []

    def gate(*args, **kwargs):
        calls.append(True)
        return {"decision": "hold"}

    monkeypatch.setattr(world_model_trust, "gate", gate)
    options = dict(memory_id="m", model_digest="a" * 64, embodiment="r", task="stop",
                   state={}, action={}, perturbations=[])
    assert safety_case.queue_gate(root, "evidence", **options)["status"] == "sim-held"
    with pytest.raises(KeyError, match="no pending"):
        safety_case.queue_gate(root, "evidence", **options)
    assert len(calls) == 1


def test_distinct_events_with_one_episode_and_replay_result_keep_separate_evidence(tmp_path):
    root = str(tmp_path)
    safety_case.add_claim(root, "stop", hazard="collision", statement="stop")
    options = dict(claim_id="stop", replay=lambda event: {"failed": True}, dependencies=[])
    event = {"failed": True, "episode_id": "episode", "hazard": "collision"}
    first = safety_case.mine(root, event, **options)
    event = {**event, "hazard": "person crossing"}
    second = safety_case.mine(root, event, **options)
    assert first["evidence_id"] != second["evidence_id"]
    assert safety_case.export_scenario(root, second["scenario_id"])["validation"]["valid"]
    assert safety_case.mine(root, event, **options) == second


def test_stale_mark_retry_repairs_an_interrupted_revalidation_queue_write(tmp_path, monkeypatch):
    root = str(tmp_path)
    safety_case.add_claim(root, "stop", hazard="collision", statement="stop")
    safety_case.add_evidence(root, "evidence", claim_id="stop", source_type="trial", source_id="run",
                            source_digest="a" * 64, dependencies=[{"kind": "model", "digest": "a" * 64}])
    append = safety_case.fp.append

    def fail_queue(root, name, record, **options):
        if name == safety_case.REVALIDATION:
            raise OSError("simulated interrupted disk write")
        return append(root, name, record, **options)

    with monkeypatch.context() as patch:
        patch.setattr(safety_case.fp, "append", fail_queue)
        with pytest.raises(OSError):
            safety_case.mark_changed(root, kind="model", digest="a" * 64)
    assert len(safety_case.mark_changed(root, kind="model", digest="a" * 64)) == 1
    assert not safety_case.mark_changed(root, kind="model", digest="a" * 64)
    assert len(safety_case.fp.journal(root, safety_case.REVALIDATION)) == 1
