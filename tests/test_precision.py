import json
import os

import pytest

from commontrace import outcome_detect, precision
from commontrace.cli import main

SAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "commons", "eval", "outcome_labelled.jsonl")


def _row(oid, truth, returncode):
    return {"occasion_id": oid, "truth": truth,
            "signals": [{"detector": "from_test_exit_code", "args": {"returncode": returncode}}]}


def test_the_metrics_are_what_their_names_say():
    rows = [_row("tp1", True, 0), _row("tp2", True, 0), _row("fp", False, 0),
            _row("tn", False, 1), _row("fn", True, 1),
            _row("undecided-success", True, 5), _row("undecided-failure", False, 5)]
    r = precision.evaluate(rows)
    assert (r.n, r.decided) == (7, 5)
    assert r.precision == pytest.approx(2 / 3) and r.failure_precision == pytest.approx(1 / 2)
    assert r.decided_share == pytest.approx(5 / 7)
    assert r.recall == pytest.approx(2 / 4)
    assert {w["occasion_id"] for w in r.wrong} == {"fp", "fn"}


def test_an_undecided_failure_costs_nothing_and_an_undecided_success_costs_recall_only():
    r = precision.evaluate([_row("a", False, 5), _row("b", True, 5), _row("c", True, 0)])
    assert r.precision == 1.0 and r.recall == pytest.approx(1 / 2)


def test_no_success_calls_means_no_precision_not_a_perfect_one(tmp_path, capsys):
    path = tmp_path / "s.jsonl"
    path.write_text(json.dumps(_row("a", True, 5)) + "\n")
    assert precision.evaluate(precision.read_sample(path.read_text())).precision is None
    assert main(["function", "precision", str(path)]) == 1
    assert "n/a" in capsys.readouterr().out


@pytest.mark.parametrize("text,message", [
    ("", "empty"), ("not json", "not JSON"), ('{"signals": [{}]}', "boolean"),
    ('{"truth": true}', "non-empty"), ('{"truth": true, "signals": []}', "non-empty")])
def test_a_bad_sample_is_refused_with_the_line(text, message):
    with pytest.raises(precision.SampleError, match=message):
        precision.read_sample(text)


def test_an_unknown_detector_is_a_sample_error_not_a_crash():
    rows = [{"occasion_id": "x", "truth": True, "signals": [{"detector": "from_vibes", "args": {}}]}]
    with pytest.raises(precision.SampleError, match="from_vibes"):
        precision.evaluate(rows)


def test_the_cli_exits_by_whether_the_target_is_met(tmp_path, capsys):
    path = tmp_path / "s.jsonl"
    path.write_text("".join(json.dumps(_row(f"o{i}", i != 0, 0)) + "\n" for i in range(5)))
    assert main(["function", "precision", str(path), "--min-precision", "0.9"]) == 1
    assert main(["function", "precision", str(path), "--min-precision", "0.75"]) == 0
    assert main(["function", "precision", str(tmp_path / "missing.jsonl")]) == 2
    capsys.readouterr()
    main(["function", "precision", str(path), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["precision"] == pytest.approx(0.8) and report["met"] is False


def test_the_shipped_regression_sample_meets_the_target_and_covers_every_detector():
    with open(SAMPLE, encoding="utf-8") as fh:
        rows = precision.read_sample(fh.read())
    result = precision.evaluate(rows)
    assert result.precision >= 0.9 and result.failure_precision >= 0.9 and not result.wrong
    used = {s["detector"] for row in rows for s in row["signals"]}
    every = {n for n in dir(outcome_detect) if n.startswith("from_") and n not in ("from_all", "from_any")}
    assert used == every, f"not covered by the sample: {sorted(every - used)}"
