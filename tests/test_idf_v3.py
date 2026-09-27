"""The opt-in idf-v3 scorer: idf-v2 with Porter stemming, and its own floor.

Pins what it buys (inflected forms match), what must not move (the default
stays idf-v2, so no store changes scorer on upgrade), how a store opts in,
and that it clears the same per-field quality gates the default is held to
at its own floor. commontrace/retrieval.py's IDF_V3_FLOOR comment has the
measurements and the one field where it is noisier.
"""
from __future__ import annotations

import importlib.util
import json
import os

from commontrace import holdout_io, retrieval, retrieval_io
from commontrace.cli import main

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(REPO, "commontrace", "fixtures", "fields")

LESSON = ("p", {"name": "retry-uploads",
                "description": "retry the failed upload with backoff",
                "status": "active"})


def _slugs(task, scorer):
    return [r.slug for r in retrieval.rank_lessons(task, [LESSON], scorer=scorer, floor=0.0)]


def test_inflected_forms_match_under_v3_only():
    task = "uploads keep failing so we are retrying"
    assert _slugs(task, retrieval.SCORER_IDF_V3) == ["retry-uploads"]
    assert _slugs(task, retrieval.SCORER_IDF_V2) == []


def test_v3_records_its_own_identity():
    [r] = retrieval.rank_lessons("failed upload", [LESSON], scorer=retrieval.SCORER_IDF_V3)
    assert r.scorer == "idf-v3"


def test_the_default_is_unchanged(tmp_path):
    """No store changes scorer on upgrade: a new scorer changes which
    lessons are eligible, and that is a new experiment."""
    assert retrieval.SCORER_IDF == retrieval.SCORER_IDF_V2 == "idf-v2"
    assert main(["init", "--dest", str(tmp_path)]) == 0
    config = retrieval_io.load_config(str(tmp_path))
    assert config.scorer == "idf-v2"
    assert config.floor == retrieval.IDF_V2_FLOOR == retrieval.DEFAULT_FLOOR


def test_opting_in_takes_v3s_own_floor_and_switching_back_restores_v2s(tmp_path):
    root = str(tmp_path)
    assert main(["init", "--dest", root]) == 0
    assert main(["retrieval", "--scorer", "idf-v3", "--dest", root]) == 0
    config = retrieval_io.load_config(root)
    assert (config.scorer, config.floor) == ("idf-v3", retrieval.IDF_V3_FLOOR)

    assert main(["retrieval", "--scorer", "idf-v2", "--dest", root]) == 0
    config = retrieval_io.load_config(root)
    assert (config.scorer, config.floor) == ("idf-v2", retrieval.IDF_V2_FLOOR)

    # An explicit floor is kept, and a floor change alone keeps the scorer.
    assert main(["retrieval", "--scorer", "idf-v3", "--floor", "0.09", "--dest", root]) == 0
    assert main(["retrieval", "--max-lessons", "4", "--dest", root]) == 0
    config = retrieval_io.load_config(root)
    assert (config.scorer, config.floor) == ("idf-v3", 0.09)


def test_a_config_file_naming_v3_without_a_floor_gets_v3s_floor(tmp_path):
    root = str(tmp_path)
    assert main(["init", "--dest", root]) == 0
    path = retrieval_io.config_path(root)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"scorer": "idf-v3"}, fh)
    config = retrieval_io.load_config(root)
    assert (config.scorer, config.floor) == ("idf-v3", retrieval.IDF_V3_FLOOR)


def test_the_holdout_log_records_v3_when_the_store_uses_it(tmp_path):
    """check_scorer_drift can only catch a mid-run switch if the label is
    the scorer that actually ran."""
    root = str(tmp_path)
    assert main(["init", "--dest", root]) == 0
    from tests.test_mcp_server import _write_lesson

    _write_lesson(root, "retry-uploads", body="Retry with backoff.",
                  description="retry the failed upload with backoff")
    assert main(["retrieval", "--scorer", "idf-v3", "--dest", root]) == 0
    holdout_io.configure(root, rate=0.5)
    assert main(["query", "--lexical", "--experiment", "--occasion-id", "o-1",
                 "--dest", root, "uploads keep failing, retrying"]) == 0
    with open(holdout_io.holdout_log_path(root), encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    assert rows and {row["scorer"] for row in rows} == {"idf-v3"}


def _field_report(scorer):
    spec = importlib.util.spec_from_file_location(
        "measure_retrieval", os.path.join(REPO, "commontrace", "reference", "measure_retrieval.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.compute(FIXTURES, scorer=scorer)


def test_v3_clears_the_quality_gates_at_its_own_floor():
    """The same bars tests/test_cross_field_retrieval.py holds the default
    to: every relevant lesson found, the right one first, and no field
    polluted past the ceiling."""
    from tests.test_cross_field_retrieval import MAX_POLLUTION

    report = _field_report(retrieval.SCORER_IDF_V3)
    assert report["floor"] == retrieval.IDF_V3_FLOOR
    for f in report["fields"]:
        assert f["recall_at_k"] == 1.0, f["field"]
        assert f["precision_at_1"] >= 0.85, f["field"]
        assert f["pollution_ratio"] <= MAX_POLLUTION, f["field"]
