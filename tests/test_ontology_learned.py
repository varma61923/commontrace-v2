"""The learned typer: deterministic training, a shipped model that loads, an off
switch, and a fallback that only speaks when the other evidence is undecided."""
import json
import os

from benchmarks import train_ontology_typing
from commontrace import ontology, ontology_classify, ontology_learned

ONTO = ontology.Ontology.default()
FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "benchmarks", "fixtures")
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
    assert any(f.startswith("ontology_typing_gemma_train.json:") for f in model.meta["fixtures"])


def test_training_fixture_shares_no_name_with_any_evaluation_fixture():
    def names(path):
        with open(os.path.join(FIXTURES, path), encoding="utf-8") as fh:
            return {r["name"].lower() for r in json.load(fh)["examples"]}
    train = names("ontology_typing_gemma_train.json")
    for held_out in ("ontology_typing_v1.json", "ontology_typing_gemma_dev.json", "ontology_typing_gemma_dev2.json",
                     "ontology_typing_gemma_test.json", "ontology_typing_gemma_test2.json",
                     "ontology_typing_gemma_test3.json"):
        assert not train & names(held_out), held_out


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
    out = tmp_path / "model.json"
    fixture = os.path.join(FIXTURES, "ontology_typing_gemma_dev.json")
    assert train_ontology_typing.main(["--fixture", fixture, "--out", str(out), "--epochs", "2"]) == 0
    model = ontology_learned.Model.from_dict(json.loads(out.read_text()))
    assert model.meta["rows"] > 100 and model.meta["epochs"] == 2
