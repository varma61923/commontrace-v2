"""The semantic-only arm honours the store's injection budget, as every other path does.

Its script appends every active lesson at or above the importance floor to its
top-k (the safety override). Without the budget, a store with many such lessons
handed an agent all of them: 3,928 lessons for one query on a 10,000-lesson
store, each read and screened first.
"""
import json
import os

import pytest

from commontrace import frontmatter, paths
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
    assert _listed(out) == [f"l{i:02d}" for i in range(10)]   # default max_lessons, in the arm's order
    assert "[commontrace] not injected:" in out and "count budget reached" in out
    assert "[commontrace] budget: 10/10 lessons" in out and f"{N - 10} not injected" in out


def test_lessons_past_a_full_budget_are_not_read(store, monkeypatch, capsys):
    main(["query", "refund", "--dest", store])  # fills the lesson cache (commontrace/lesson_cache.py)
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
