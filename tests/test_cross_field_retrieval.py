import json
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(REPO_ROOT, "commontrace", "fixtures", "fields")

sys.path.insert(0, os.path.join(REPO_ROOT, "commontrace", "reference"))
import measure_retrieval  # noqa: E402

from commontrace import retrieval  # noqa: E402

MAX_POLLUTION = 2.4
MAX_SPREAD = 2.0


@pytest.fixture(scope="module")
def report():
    return measure_retrieval.compute(FIXTURES)


class TestTheCorpusItself:
    def test_it_covers_fields_with_no_starter_vocabulary(self, report):
        fields = {f["field"] for f in report["fields"]}
        assert {"robotics", "legal", "clinical", "finance"} <= fields
        assert len(fields) >= 8

    def test_every_field_has_enough_queries_to_mean_something(self, report):
        for f in report["fields"]:
            assert f["n_queries"] >= 12, f"{f['field']} has too few labelled queries"
            assert f["n_lessons"] >= 5

    def test_every_labelled_query_names_a_lesson_that_exists(self):
        for doc in measure_retrieval.load_fields(FIXTURES):
            slugs = {lesson["name"] for lesson in doc["lessons"]}
            for case in doc["queries"]:
                missing = set(case["relevant"]) - slugs
                assert not missing, f"{doc['field']}: {case['query']!r} names {missing}"


class TestTheGate:
    def test_no_field_is_polluted_beyond_the_ceiling(self, report):
        over = {
            f["field"]: f["pollution_ratio"] for f in report["fields"]
            if f["pollution_ratio"] and f["pollution_ratio"] > MAX_POLLUTION
        }
        assert not over, (
            f"pollution over {MAX_POLLUTION}x in {over}. Every retrieval beyond what "
            "the query is about becomes an eligible holdout assignment, so this is "
            "measurement error entering the causal estimate."
        )

    def test_no_field_is_much_worse_than_the_best(self, report):
        assert report["pollution_spread"] <= MAX_SPREAD, (
            f"retrieval is {report['pollution_spread']:.2f}x worse in "
            f"{report['worst_field']!r} than in the best field"
        )

    def test_quality_holds_in_every_field(self, report):
        for f in report["fields"]:
            assert f["recall_at_k"] == 1.0, f"{f['field']} loses relevant lessons"
            assert f["precision_at_1"] >= 0.85, f"{f['field']} ranks the wrong lesson first"


class TestTheGateActuallyCatchesTheRegressionItExistsFor:
    def test_the_historical_scorer_fails_the_ceiling(self):
        old = measure_retrieval.compute(
            FIXTURES, floor=0.0, scorer=retrieval.SCORER_COUNT)
        worst = max(f["pollution_ratio"] for f in old["fields"])
        assert worst > MAX_POLLUTION, (
            "the pre-IDF scorer should exceed the pollution ceiling; a gate that "
            "passes it is measuring nothing"
        )

    def test_the_current_scorer_beats_it_in_every_single_field(self):
        old = {f["field"]: f["pollution_ratio"] for f in measure_retrieval.compute(
            FIXTURES, floor=0.0, scorer=retrieval.SCORER_COUNT)["fields"]}
        new = {f["field"]: f["pollution_ratio"] for f in measure_retrieval.compute(
            FIXTURES)["fields"]}
        regressed = {k: (old[k], new[k]) for k in old if new[k] > old[k]}
        assert not regressed, f"fields regressed (old, new): {regressed}"


class TestItRunsAsACommand:
    def test_bench_retrieval_emits_parseable_json(self, tmp_path):
        result = subprocess.run(
            [sys.executable, "-m", "commontrace", "bench", "--retrieval", "--json",
             "--dest", str(tmp_path)],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["fields"]
        assert payload["scorer"] == retrieval.SCORER_IDF

    def test_the_gate_flags_are_honoured_by_the_exit_code(self, tmp_path):
        result = subprocess.run(
            [sys.executable, "-m", "commontrace", "bench", "--retrieval",
             "--max-pollution", "0.5", "--dest", str(tmp_path)],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        assert result.returncode != 0
        assert "pollution exceeds" in result.stderr

    def test_retrieval_and_pilot_are_not_silently_combined(self, tmp_path):
        result = subprocess.run(
            [sys.executable, "-m", "commontrace", "bench", "--retrieval", "--pilot",
             "--dest", str(tmp_path)],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        assert result.returncode == 2
        assert "measure different things" in result.stderr
