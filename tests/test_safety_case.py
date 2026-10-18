import pytest

from commontrace import safety_case


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
