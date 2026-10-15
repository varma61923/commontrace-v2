"""The learned typer: deterministic training, a shipped model that loads, an off
switch, and a fallback that only speaks when the other evidence is undecided."""
import json

from benchmarks import train_ontology_typing
from commontrace import ontology, ontology_classify, ontology_learned

ONTO = ontology.Ontology.default()
ROWS = [("Brannigan", "Brannigan approved the change.", "person"),
        ("Tarnhold", "She moved to Tarnhold last spring.", "place"),
        ("Velkor", "Velkor approved the budget.", "person"),
        ("Ostmark", "They moved to Ostmark in May.", "place"),
        ("Latency", "Latency matters a lot.", "none")] * 4


def test_training_is_deterministic_and_round_trips():
    a = ontology_learned.train(ROWS, epochs=5)
    b = ontology_learned.train(ROWS, epochs=5)
    assert a.to_dict() == b.to_dict()
    again = ontology_learned.Model.from_dict(json.loads(json.dumps(a.to_dict())))
    probs = again.probabilities("Quillon", "Quillon approved the plan.")
    assert max(probs, key=probs.get) == "person" and abs(sum(probs.values()) - 1) < 1e-9


def test_the_shipped_model_loads_and_names_its_training_data():
    model = ontology_learned.load()
    assert model is not None and set(model.labels) >= {"person", "service", "none"}
    assert model.meta["fixtures"] and model.meta["rows"] > 100  # provenance of the shipped weights


def test_the_learned_typer_is_a_fallback_and_can_be_switched_off(monkeypatch):
    decided = ontology_classify.classify("src/app/main.py", "Edit src/app/main.py.", onto=ONTO, methods=("keyword",))
    assert "learned" not in {s for s, _t, _c in decided.evidence}  # the rules already decided
    found = ontology_classify.classify("Quorandel", "We met Quorandel at the offsite and she said yes.",
                                       onto=ONTO, methods=("keyword",))
    assert found.type == "person"
    monkeypatch.setenv("COMMONTRACE_TYPING_MODEL", "off")
    off = ontology_classify.classify("Quorandel", "We met Quorandel at the offsite and she said yes.",
                                     onto=ONTO, methods=("keyword",))
    assert "learned" not in {s for s, _t, _c in off.evidence}


def test_the_trainer_cli_writes_a_loadable_model(tmp_path):
    rows = [{"id": f"r{i}", "name": n, "context": c, "expected": label, "slice": "generated"}
            for i, (n, c, label) in enumerate(ROWS)]
    fixture = tmp_path / "rows.json"
    fixture.write_text(json.dumps({"schema_version": "1.0.0", "examples": rows}), encoding="utf-8")
    out = tmp_path / "model.json"
    assert train_ontology_typing.main(["--fixture", str(fixture), "--out", str(out), "--epochs", "2"]) == 0
    model = ontology_learned.Model.from_dict(json.loads(out.read_text()))
    assert model.meta["rows"] == len(ROWS) and model.meta["epochs"] == 2
