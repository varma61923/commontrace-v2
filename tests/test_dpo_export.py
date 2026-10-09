"""Preference pairs only from decisive randomized compression trials."""
import json

from commontrace import compression, memory_authority, trace_io
from commontrace.cli import main


def _trial(root, proposal_id, wins):
    experiment = compression.register(root, proposal_id, trials=300, seed="fixed")
    for _ in range(300):
        trial = compression.next_trial(root, experiment["id"])
        compression.outcome(root, trial["id"], float((trial["arm"] == "candidate") == wins))
    return experiment


def test_pairs_follow_the_randomized_verdict_and_respect_forgetting(tmp_path, capsys):
    root = str(tmp_path)
    tid = trace_io.read(trace_io.write_new(root, title="case", context="release", solution="step one",
                                           tags=["test"]))[0]["id"]
    row = compression.propose(root, "Follow verified release steps", level="episode", sources=[tid],
                              actor="author", applies_when="releasing a service", do_not_apply_when="other")
    assert compression.export_preferences(root)["pairs"] == []  # registered nothing yet
    good = _trial(root, row["id"], wins=True)
    pairs = compression.export_preferences(root)["pairs"]
    assert {p["evidence"]["against"] for p in pairs} == {"parent", "raw"}
    assert all(p["chosen"] == "Follow verified release steps" and p["evidence"]["effect"] > 0
               and p["evidence"]["randomized"] and p["prompt"] == "releasing a service" for p in pairs)
    bad = _trial(root, row["id"], wins=False)
    reversed_pairs = [p for p in compression.export_preferences(root)["pairs"] if p["evidence"]["experiment"] == bad["id"]]
    assert reversed_pairs and all(p["rejected"] == "Follow verified release steps" for p in reversed_pairs)
    assert main(["compression", "export-training", "--format", "dpo", "--dest", root]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["format"] == "evidence-linked-dpo" and out["training_performed"] is False
    assert {p["evidence"]["experiment"] for p in out["pairs"]} == {good["id"], bad["id"]}
    memory_authority.record_forgetting(root, tid, forgotten=True)
    assert compression.export_preferences(root)["pairs"] == []
