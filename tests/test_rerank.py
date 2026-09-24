"""The opt-in cross-encoder reranker (commontrace/rerank_arm.py).

A deterministic fake stands in for the cross-encoder -- the real model is
not a test dependency -- scoring a pair by how many of a few marker words
the lesson text shares with the task. What is tested is everything around
it: that it only reorders the first stage's pool, that both surfaces rerank
the same pool the same way and log it under the same label, that a store
that cannot rerank ranks exactly as if it had not asked, and that harm
withdrawal names only what the reranked page would have shown.
"""
from __future__ import annotations

import json

import pytest

from commontrace import holdout_io, integrity, rerank_arm, retrieval_io, semantic_arm
from commontrace.cli import main
from commontrace.commands import query_cmd
from tests import test_mcp_fusion as fusion_tests
from tests.test_hybrid_retrieval import _args
from tests.test_mcp_fusion import SEMANTIC, TASK, _logged, _slugs, _stub_both
from tests.test_mcp_server import _write_lesson, call

pytest.importorskip("mcp", reason="`commontrace serve` needs the MCP SDK: pip install 'commontrace[serve]'")

from commontrace import mcp_server  # noqa: E402

MARKERS = ("opt-out", "roster", "vendor")

store = fusion_tests.store


class FakeCrossEncoder:
    calls = 0

    def predict(self, pairs, **_kw):
        FakeCrossEncoder.calls += 1
        return [float(sum(m in text.lower() for m in MARKERS)) for _task, text in pairs]


@pytest.fixture
def ce(monkeypatch):
    FakeCrossEncoder.calls = 0
    monkeypatch.setattr(rerank_arm, "available", lambda: True)
    monkeypatch.setattr(rerank_arm, "_load", lambda *_a: FakeCrossEncoder())
    return FakeCrossEncoder


# --- the stage itself --------------------------------------------------------

def test_it_reorders_the_pool_and_keeps_the_page(ce):
    texts = {"a": "nothing", "b": "vendor", "c": "opt-out roster vendor", "d": "roster"}
    page, _ = rerank_arm.rerank("t", ["a", "b", "c", "d"], texts, 2)
    assert [s for s, _ in page] == ["c", "b"] or [s for s, _ in page] == ["c", "d"]
    assert page[0] == ("c", 3.0)


def test_ties_keep_the_first_stage_order(ce):
    texts = {s: "vendor" for s in "abcd"}
    page, _ = rerank_arm.rerank("t", ["b", "a", "d", "c"], texts, 3)
    assert [s for s, _ in page] == ["b", "a", "d"]


def test_it_never_adds_a_lesson(ce):
    texts = {"a": "x", "b": "vendor", "outsider": "opt-out roster vendor"}
    page, _ = rerank_arm.rerank("t", ["a", "b"], texts, 5)
    assert {s for s, _ in page} == {"a", "b"}


def test_a_withdrawn_lesson_is_named_only_if_it_would_have_made_the_page(ce):
    texts = {"a": "vendor", "b": "roster", "c": "x", "hi": "opt-out roster vendor", "lo": "x"}
    page, named = rerank_arm.rerank("t", ["a", "b", "c"], texts, 2, withdrawn=["hi", "lo"])
    assert [s for s, _ in page] == ["a", "b"]
    assert named == ["hi"]


# --- settings and labels -----------------------------------------------------

def test_the_label_records_the_reranker_around_the_first_stage():
    label = retrieval_io.eligibility_label("idf-v2", retrieval_io.FUSION_RRF, retrieval_io.RERANK_CE)
    assert label == "ce:minilm6(rrf(idf-v2+semantic))"
    assert retrieval_io.parse_eligibility_label(label) == ("idf-v2", retrieval_io.FUSION_RRF)
    assert retrieval_io.parse_rerank_label(label) == ("rrf(idf-v2+semantic)", retrieval_io.RERANK_CE)
    assert retrieval_io.parse_rerank_label("idf-v2") == ("idf-v2", retrieval_io.RERANK_NONE)


def test_a_reranked_row_is_not_judged_against_the_lexical_floor():
    rows = [integrity.Assignment(lesson="x", occasion_id=f"o{i}", injected=bool(i % 2),
                                 relevance=-3.0, scorer="ce:minilm6(idf-v2)", floor=0.04)
            for i in range(20)]
    assert integrity.check_marginal_eligibility(rows).severity == integrity.SEVERITY_OK


def test_configuring_it_is_persisted_and_pinned_from_the_log(tmp_path, capsys):
    root = str(tmp_path)
    assert main(["init", "--dest", root]) == 0
    assert main(["retrieval", "--rerank", "cross-encoder", "--dest", root]) == 0
    assert retrieval_io.load_config(root).rerank == retrieval_io.RERANK_CE
    assert "rerank=cross-encoder" in capsys.readouterr().out
    with pytest.raises(ValueError):
        retrieval_io.configure(root, rerank="llm")

    # An unconfigured store keeps what its log says it ran, reranker included.
    import os

    os.remove(retrieval_io.config_path(root))
    with open(holdout_io.holdout_log_path(root), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"occasion_id": "o", "lesson": "l", "injected": True, "rate": 0.5,
                             "salt": "s", "scorer": "ce:minilm6(rrf(idf-v2+semantic))",
                             "floor": 0.04}) + "\n")
    pinned = retrieval_io.load_config(root)
    assert (pinned.scorer, pinned.fusion, pinned.rerank) == (
        "idf-v2", retrieval_io.FUSION_RRF, retrieval_io.RERANK_CE)
    assert pinned.eligibility == "ce:minilm6(rrf(idf-v2+semantic))"


# --- both surfaces -----------------------------------------------------------

def _rerank_store(store):
    # A lesson the first stage ranks below the page (it matches the task on
    # one word) but the cross-encoder puts first.
    for i in range(4):
        _write_lesson(store, f"filler-{i}", body="Nothing relevant.",
                      description=f"password reset email filler {i}")
    _write_lesson(store, "roster-drift", body="Resync nightly.",
                  description="suppression roster drifts at the vendor")
    retrieval_io.configure(store, rerank=retrieval_io.RERANK_CE)
    return store


def test_the_reranker_lifts_a_lesson_from_the_pool_onto_the_page(store, ce, monkeypatch):
    _stub_both(monkeypatch)
    _rerank_store(store)
    retrieval_io.configure(store, fusion=retrieval_io.FUSION_NONE)
    lexical_only = call(mcp_server.build_server(store), "retrieve", task=TASK, top_k=2,
                        occasion_id="r-1")
    # The logged rank, not `lessons`: the fixture runs a 50% holdout, so the
    # top lesson may be in either arm.
    rows = _logged(store, "r-1")
    assert rows["roster-drift"]["rank"] == 1, rows
    assert {r["scorer"] for r in rows.values()} == {"ce:minilm6(idf-v2)"}
    assert rows["roster-drift"]["relevance"] == 2.0

    # Without the reranker the first stage keeps it off the page.
    retrieval_io.configure(store, rerank=retrieval_io.RERANK_NONE)
    plain = call(mcp_server.build_server(store), "retrieve", task=TASK, top_k=2)
    assert "roster-drift" not in _slugs(plain, "lessons", "withheld")
    assert "rerank_note" not in lexical_only


def test_both_surfaces_rerank_the_same_pool_the_same_way(store, ce, monkeypatch):
    _stub_both(monkeypatch)
    _rerank_store(store)
    assert query_cmd.run(_args(store, TASK, experiment=True, occasion_id="cli-r", top_k=2)) == 0
    call(mcp_server.build_server(store), "retrieve", task=TASK, top_k=2, occasion_id="mcp-r")
    cli_rows, mcp_rows = _logged(store, "cli-r"), _logged(store, "mcp-r")
    assert set(cli_rows) == set(mcp_rows) and cli_rows
    for slug in cli_rows:
        for field in ("scorer", "floor", "relevance"):
            assert cli_rows[slug][field] == mcp_rows[slug][field], (slug, field)
    assert {r["scorer"] for r in mcp_rows.values()} == {"ce:minilm6(rrf(idf-v2+semantic))"}


def test_a_store_that_cannot_rerank_ranks_as_if_it_had_not_asked(store, monkeypatch):
    _stub_both(monkeypatch)
    _rerank_store(store)
    monkeypatch.setattr(rerank_arm, "available", lambda: False)
    server = mcp_server.build_server(store)
    out = call(server, "retrieve", task=TASK, top_k=2, occasion_id="no-ce")
    assert "attention extra" in out["rerank_note"]
    assert {r["scorer"] for r in _logged(store, "no-ce").values()} == {"rrf(idf-v2+semantic)"}

    retrieval_io.configure(store, rerank=retrieval_io.RERANK_NONE)
    plain = call(server, "retrieve", task=TASK, top_k=2, occasion_id="plain")
    assert _slugs(out, "lessons", "withheld") == _slugs(plain, "lessons", "withheld")
    assert {r: v["relevance"] for r, v in _logged(store, "no-ce").items()} == {
        r: v["relevance"] for r, v in _logged(store, "plain").items()}


def test_the_model_runs_once_per_retrieval(store, ce, monkeypatch):
    _stub_both(monkeypatch)
    _rerank_store(store)
    call(mcp_server.build_server(store), "retrieve", task=TASK, top_k=2)
    assert ce.calls == 1


def test_semantic_ranking_stub_is_the_fusion_one():
    # Guards the fixture this file borrows: the semantic arm surfaces the
    # lesson only the cross-encoder can lift.
    assert "unsubscribe-sync" in SEMANTIC and semantic_arm is not None


def test_withdrawal_names_only_what_the_reranked_page_would_have_shown(store, ce, monkeypatch):
    """Both surfaces hand the reranker the withdrawn lessons found in the
    pool, and name only those whose score would have put them on the page."""
    from commontrace import evidence

    _stub_both(monkeypatch)
    _rerank_store(store)
    retrieval_io.configure(store, fusion=retrieval_io.FUSION_NONE)
    _write_lesson(store, "vendor-optout", body="x",
                  description="password reset opt-out roster at the vendor")
    hurts = {"verdict": "HURTS", "effect": -0.3, "ci_95": [-0.5, -0.1], "n_treated": 40,
             "n_control": 40}
    monkeypatch.setattr(evidence, "withdrawn",
                        lambda root, policy: {"vendor-optout": hurts, "filler-2": hurts})

    out = call(mcp_server.build_server(store), "retrieve", task=TASK, top_k=1)
    assert _slugs(out, "lessons", "withheld") == {"roster-drift"}
    assert {item["slug"] for item in out["withdrawn"]} == {"vendor-optout"}

    monkeypatch.setattr(query_cmd, "has_attention_deps", lambda: False)
    printed = []
    monkeypatch.setattr(query_cmd, "_print_withdrawn", lambda slugs, harmful: printed.extend(slugs))
    assert query_cmd.run(_args(store, TASK, top_k=1, lexical=True)) == 0
    assert printed == ["vendor-optout"]


def test_every_caller_reranks_the_same_capped_text(ce, monkeypatch):
    seen = []

    class Recording(FakeCrossEncoder):
        def predict(self, pairs, **kw):
            seen.extend(text for _t, text in pairs)
            return super().predict(pairs, **kw)

    monkeypatch.setattr(rerank_arm, "_load", lambda *_a: Recording())
    rerank_arm.rerank("t", ["a"], {"a": "x" * (rerank_arm.MAX_CHARS * 3)}, 1)
    assert seen == ["x" * rerank_arm.MAX_CHARS]


def test_the_fast_model_is_its_own_treatment(store, ce, monkeypatch):
    """A different model is a different ranking: the label names it, and a
    store pinned from its log comes back on the same model."""
    loaded = []
    monkeypatch.setattr(rerank_arm, "_load", lambda mode="": loaded.append(mode) or FakeCrossEncoder())
    _stub_both(monkeypatch)
    _rerank_store(store)
    retrieval_io.configure(store, rerank=retrieval_io.RERANK_CE_FAST)
    call(mcp_server.build_server(store), "retrieve", task=TASK, top_k=2, occasion_id="fast-1")
    assert {r["scorer"] for r in _logged(store, "fast-1").values()} == {
        "ce:tinybert2(rrf(idf-v2+semantic))"}
    assert set(loaded) == {retrieval_io.RERANK_CE_FAST}
    assert retrieval_io.parse_rerank_label("ce:tinybert2(idf-v2)") == (
        "idf-v2", retrieval_io.RERANK_CE_FAST)



# --- the default -------------------------------------------------------------

@pytest.fixture
def no_override(monkeypatch):
    monkeypatch.delenv(retrieval_io.DEFAULT_RERANK_ENV, raising=False)


def test_a_new_store_reranks_by_default_when_the_model_is_installed(tmp_path, no_override, monkeypatch):
    monkeypatch.setattr(rerank_arm, "available", lambda: True)
    assert main(["init", "--dest", str(tmp_path)]) == 0
    assert retrieval_io.load_config(str(tmp_path)).rerank == retrieval_io.RERANK_CE_FAST
    monkeypatch.setattr(rerank_arm, "available", lambda: False)
    assert retrieval_io.load_config(str(tmp_path)).rerank == retrieval_io.RERANK_NONE


def test_a_store_mid_experiment_does_not_gain_a_reranker_on_upgrade(tmp_path, no_override, monkeypatch):
    """Configured before reranking existed, with assignments logged under the
    lexical label: it stays lexical, configured file or not."""
    monkeypatch.setattr(rerank_arm, "available", lambda: True)
    root = str(tmp_path)
    assert main(["init", "--dest", root]) == 0
    retrieval_io.configure(root, floor=0.05)
    config_file = retrieval_io.config_path(root)
    raw = json.load(open(config_file))
    raw.pop("rerank")
    json.dump(raw, open(config_file, "w"))
    with open(holdout_io.holdout_log_path(root), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"occasion_id": "o", "lesson": "l", "injected": True, "rate": 0.5,
                             "salt": "s", "scorer": "idf-v2", "floor": 0.05}) + "\n")
    assert retrieval_io.load_config(root).rerank == retrieval_io.RERANK_NONE
    import os

    os.remove(config_file)
    assert retrieval_io.load_config(root).rerank == retrieval_io.RERANK_NONE


def test_a_default_reranked_store_stays_reranked_once_it_has_history(tmp_path, no_override, monkeypatch):
    monkeypatch.setattr(rerank_arm, "available", lambda: True)
    root = str(tmp_path)
    assert main(["init", "--dest", root]) == 0
    with open(holdout_io.holdout_log_path(root), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"occasion_id": "o", "lesson": "l", "injected": True, "rate": 0.5,
                             "salt": "s", "scorer": "ce:tinybert2(idf-v2)", "floor": 0.04}) + "\n")
    assert retrieval_io.load_config(root).rerank == retrieval_io.RERANK_CE_FAST


def test_the_operator_can_turn_the_default_off(tmp_path, monkeypatch):
    monkeypatch.setattr(rerank_arm, "available", lambda: True)
    monkeypatch.setenv(retrieval_io.DEFAULT_RERANK_ENV, "none")
    assert main(["init", "--dest", str(tmp_path)]) == 0
    assert retrieval_io.load_config(str(tmp_path)).rerank == retrieval_io.RERANK_NONE
