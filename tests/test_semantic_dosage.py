import json
import os

import pytest

from commontrace import frontmatter, holdout_io, integrity, paths, retrieval, retrieval_io
from commontrace.cli import main
from commontrace.commands import query_cmd
from commontrace.commands.experiment_cmd import holdout_log_path

N = 40


def _write(root, slug, *, importance=5, core=False):
    fm = {"name": slug, "status": "active", "description": f"{slug} about refunds",
          "importance": importance, "agent_type": "code"}
    if core:
        fm["core"] = True
    frontmatter.write(os.path.join(paths.lessons_dir(root), f"lesson_{slug}.md"), fm,
                      f"## Rule\nRule for {slug}.\n")


@pytest.fixture
def store(tmp_path, monkeypatch, capsys):
    root = str(tmp_path / "store")
    main(["init", "--agent-type", "code", "--dest", root])
    for i in range(N):
        _write(root, f"l{i:02d}")
    stdout = "# Top-3 retrieval (+ importance>=4 override)\n# Query: 'refund'\n" + "".join(
        f"l{i:02d} | cosine={0.9 - i * 0.01:.3f} | importance=5\n" for i in range(N))
    monkeypatch.setattr(query_cmd, "has_attention_deps", lambda: True)
    monkeypatch.setattr(query_cmd, "_index_is_unusable", lambda root: "")
    monkeypatch.setattr(query_cmd, "run_script",
                        lambda root, rel, args, hint, capture=False: (0, stdout) if capture else 0)
    capsys.readouterr()
    return root


def _listed(out):
    return [line.split("|")[0].strip() for line in out.splitlines()
            if "|" in line and not line.startswith("#")]


def test_the_override_is_cut_to_the_budget_and_the_rest_is_named(store, capsys):
    assert main(["query", "refund", "--dest", store]) == 0
    out = capsys.readouterr().out
    assert _listed(out) == [f"l{i:02d}" for i in range(10)]
    assert "[commontrace] not injected:" in out and "count budget reached" in out
    assert "[commontrace] budget: 10/10 lessons" in out and f"{N - 10} not injected" in out


def test_lessons_past_a_full_budget_are_not_read(store, monkeypatch, capsys):
    main(["query", "refund", "--dest", store])
    capsys.readouterr()
    reads = []
    real = frontmatter.read
    monkeypatch.setattr(frontmatter, "read", lambda p, *a, **k: reads.append(p) or real(p, *a, **k))
    assert main(["query", "refund", "--dest", store]) == 0
    full_reads = [p for p in reads if os.path.basename(p).startswith("lesson_l")]
    assert len(full_reads) <= 20 < N
    assert f"{N - 20} more (count budget reached)" in capsys.readouterr().out


def test_a_core_lesson_is_admitted_first(store, capsys):
    _write(store, "always", importance=3, core=True)
    assert main(["query", "refund", "--dest", store]) == 0
    out = capsys.readouterr().out
    assert "always | core | importance=3" in out
    assert len(_listed(out)) == 10


def test_only_what_is_injected_is_given_an_arm(store, capsys):
    assert main(["query", "refund", "--experiment", "--occasion-id", "o1",
                 "--holdout-rate", "0.5", "--dest", store]) == 0
    logged = {json.loads(line)["lesson"] for line in open(holdout_log_path(store), encoding="utf-8")}
    assert logged == {f"l{i:02d}" for i in range(10)}


def test_a_wider_budget_admits_more(store, capsys):
    main(["retrieval", "--max-lessons", "25", "--max-chars", "100000", "--dest", store])
    capsys.readouterr()
    assert main(["query", "refund", "--dest", store]) == 0
    assert len(_listed(capsys.readouterr().out)) == 25


def _labels(root):
    return [json.loads(line).get("scorer") for line in open(holdout_log_path(root), encoding="utf-8")]


def test_a_new_experiment_records_the_budgeted_treatment(store, capsys):
    assert main(["query", "refund", "--experiment", "--occasion-id", "o1", "--dest", store]) == 0
    assert set(_labels(store)) == {retrieval_io.SEMANTIC_ONLY_DOSED}


def test_an_experiment_already_on_the_unbudgeted_treatment_keeps_it(store, capsys):
    holdout_io.assign_and_log(store, ["l00"], occasion_id="before-upgrade", rate=0.5, salt="s",
                              scorer=retrieval_io.SEMANTIC_ONLY, floor=0.0)
    assert retrieval_io.semantic_only_undosed_pinned(store)
    assert main(["query", "refund", "--experiment", "--occasion-id", "o2", "--dest", store]) == 0
    out = capsys.readouterr().out
    assert len(_listed(out)) == N
    assert set(_labels(store)) == {retrieval_io.SEMANTIC_ONLY}
    assert main(["query", "refund", "--dest", store]) == 0
    assert len(_listed(capsys.readouterr().out)) == N


def test_the_two_treatments_are_never_pooled_silently():
    rows = [integrity.Assignment(lesson="a", occasion_id=f"o{i}", injected=bool(i % 2), salt="s",
                                 scorer=label, floor=0.0)
            for i, label in enumerate([retrieval_io.SEMANTIC_ONLY, retrieval_io.SEMANTIC_ONLY_DOSED] * 3)]
    assert integrity.check_scorer_drift(rows).severity == integrity.SEVERITY_INVALIDATES


def test_the_new_label_reads_as_semantic_only():
    label = retrieval_io.semantic_only_label("arctic-m", dosed=True)
    assert label == "semantic-dosed@arctic-m"
    assert retrieval_io.parse_embedder(label) == "arctic-m"
    assert retrieval_io.parse_eligibility_label(label) == (retrieval.SCORER_IDF, retrieval_io.FUSION_NONE)
    assert retrieval_io.semantic_only_label("arctic-m") == "semantic@arctic-m"


def test_a_semantic_only_log_pins_the_store_to_what_it_ran(tmp_path):
    root = str(tmp_path / "pinned")
    main(["init", "--agent-type", "code", "--dest", root])
    holdout_io.assign_and_log(root, ["a"], occasion_id="o", rate=0.5, salt="s",
                              scorer=retrieval_io.semantic_only_label("arctic-m"))
    config = retrieval_io.load_config(root)
    assert config.pinned_for_running_experiment
    assert config.scorer == retrieval.SCORER_IDF and config.fusion == retrieval_io.FUSION_NONE
    assert retrieval_io.logged_embedding_model(root) == "Snowflake/snowflake-arctic-embed-m-v1.5"
    assert retrieval_io.semantic_only_undosed_pinned(root)
