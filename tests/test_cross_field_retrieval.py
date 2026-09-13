"""Field-agnosticism, as a gate rather than a claim.

Retrieval was tuned and reasoned about on coding examples, and regressed the
other fields silently, because nothing measured them. Raw word-overlap scoring
gave wordier fields (legal, robotics) systematically higher scores than terse
ones, so no single threshold meant the same thing in two stores -- and no test
would have caught a change that improved coding at legal's expense.

This runs the labelled cross-field corpus in commontrace/fixtures/fields/
(eight fields, 48 lessons, 144 queries) and gates on two things, deliberately
neither of them a mean:

  - the WORST field's pollution ratio, absolutely; and
  - the SPREAD between the worst and best field.

Both are needed. Against the historical scorer the eight fields polluted at
1.72x-2.50x. The current scorer's DEFAULT_FLOOR is not tuned against this
corpus alone -- commontrace/retrieval.py's comment on DEFAULT_FLOOR documents
a second corpus (commons/eval/, pinned by hub/tests/test_commons.py) whose
existing thresholds bound how high the floor can go. Jointly, the floor lands
at 1.72x-2.33x here: real pollution reduction, but well short of what tuning
against this corpus alone would have bought (1.00x-1.28x at the higher,
single-corpus floor). The ceiling below sits between those two scorers'
worst fields on purpose, so this gate still catches the historical scorer
while accepting the joint-calibrated one.
"""
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

# Sits between the joint-calibrated scorer's worst field (legal, 2.33x) and
# the historical scorer's (legal, 2.50x) -- see commontrace/retrieval.py's
# DEFAULT_FLOOR comment for why the floor can't be tuned tighter against this
# corpus alone. Headroom over 2.33x so an ordinary tuning change does not
# fail CI, while the historical scorer still fails the ceiling.
MAX_POLLUTION = 2.4
MAX_SPREAD = 2.0


@pytest.fixture(scope="module")
def report():
    return measure_retrieval.compute(FIXTURES)


class TestTheCorpusItself:
    def test_it_covers_fields_with_no_starter_vocabulary(self, report):
        """robotics, legal, clinical and finance are the point: none has a
        STARTER_DOMAINS entry, and before the taxonomy was opened none could
        even be declared as an agent_type.

        The floor ratchets rather than sitting at whatever the corpus
        happens to hold -- "six fields is not any field" is answered by
        adding fields, so this must fail if one is ever deleted."""
        fields = {f["field"] for f in report["fields"]}
        assert {"robotics", "legal", "clinical", "finance"} <= fields
        assert len(fields) >= 8

    def test_every_field_has_enough_queries_to_mean_something(self, report):
        for f in report["fields"]:
            assert f["n_queries"] >= 12, f"{f['field']} has too few labelled queries"
            assert f["n_lessons"] >= 5

    def test_every_labelled_query_names_a_lesson_that_exists(self):
        """A typo in a fixture would show up as a permanent recall failure
        attributed to the retriever."""
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
        """If this ever passes, the gate has stopped working -- not the
        scorer having improved."""
        old = measure_retrieval.compute(
            FIXTURES, floor=0.0, scorer=retrieval.SCORER_COUNT)
        worst = max(f["pollution_ratio"] for f in old["fields"])
        assert worst > MAX_POLLUTION, (
            "the pre-IDF scorer should exceed the pollution ceiling; a gate that "
            "passes it is measuring nothing"
        )

    def test_the_current_scorer_beats_it_in_every_single_field(self):
        """Not on average. A change that helped the mean while regressing one
        field is exactly what a cross-field benchmark is for."""
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
        """A gate that always exits 0 looks wired up and enforces nothing --
        the failure `bench --pilot --strict` was fixed for."""
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
