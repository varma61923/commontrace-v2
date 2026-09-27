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
    assert retrieval_io.load_config(str(tmp_path)).rerank == retrieval_io.RERANK_CE
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


def test_without_fusion_both_surfaces_rerank_the_lexical_arm(store, ce, monkeypatch):
    """With the attention extra installed and fusion off, `query` used to rank
    semantically while MCP ranked lexically; a reranking store now runs the
    same reranked lexical ranking on both, under one label."""
    _stub_both(monkeypatch)
    _rerank_store(store)
    retrieval_io.configure(store, fusion=retrieval_io.FUSION_NONE,
                           rerank=retrieval_io.RERANK_CE_FAST)
    monkeypatch.setattr(query_cmd, "run_script",
                        lambda *a, **k: pytest.fail("the semantic arm must not run"))
    assert query_cmd.run(_args(store, TASK, experiment=True, occasion_id="cli-l", top_k=2)) == 0
    call(mcp_server.build_server(store), "retrieve", task=TASK, top_k=2, occasion_id="mcp-l")
    cli_rows, mcp_rows = _logged(store, "cli-l"), _logged(store, "mcp-l")
    assert set(cli_rows) == set(mcp_rows) and cli_rows
    for slug in cli_rows:
        for field in ("scorer", "relevance", "rank"):
            assert cli_rows[slug][field] == mcp_rows[slug][field], (slug, field)
    assert {r["scorer"] for r in cli_rows.values()} == {"ce:tinybert2(idf-v2)"}


def test_a_store_that_ran_semantic_retrieval_stays_semantic(tmp_path, no_override, monkeypatch):
    monkeypatch.setattr(rerank_arm, "available", lambda: True)
    root = str(tmp_path)
    assert main(["init", "--dest", root]) == 0
    with open(holdout_io.holdout_log_path(root), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"occasion_id": "o", "lesson": "l", "injected": True, "rate": 0.5,
                             "salt": "s", "scorer": "semantic", "floor": 0.04}) + "\n")
    assert retrieval_io.load_config(root).rerank == retrieval_io.RERANK_NONE


# --- gated fusion --------------------------------------------------------------

class GateCrossEncoder(FakeCrossEncoder):
    """Scores 3 per marker word, minus 5: a lesson with no marker scores -5,
    below the gate (-4); one with a marker scores -2 or more, above it."""

    def predict(self, pairs, **kw):
        return [3.0 * x - 5.0 for x in super().predict(pairs, **kw)]


@pytest.fixture
def gate_ce(monkeypatch):
    monkeypatch.setattr(rerank_arm, "available", lambda: True)
    monkeypatch.setattr(rerank_arm, "_load", lambda *_a: GateCrossEncoder())


def test_the_gate_filters_the_pool_and_the_withdrawn():
    admit = rerank_arm.admit_gated({"cleared"}, "cross-encoder")
    assert admit("cleared", -50.0) and admit("vouched", -4.0) and not admit("other", -4.1)


def test_gated_fusion_admits_what_the_reranker_vouches_for(store, gate_ce, monkeypatch):
    """suppression-list cleared the lexical floor: admitted though it scores
    -5. unsubscribe-sync only the semantic arm found, and the reranker vouches
    for it (-2): admitted. refund-threshold, also semantic-only, scores -5:
    kept off the page, where plain fusion would have put it."""
    _stub_both(monkeypatch)
    retrieval_io.configure(store, fusion=retrieval_io.FUSION_GATED,
                           rerank=retrieval_io.RERANK_CE)
    out = call(mcp_server.build_server(store), "retrieve", task=TASK, occasion_id="g-1")
    page = _slugs(out, "lessons", "withheld")
    assert {"suppression-list", "unsubscribe-sync"} <= page
    assert "refund-threshold" not in page
    assert {r["scorer"] for r in _logged(store, "g-1").values()} == {
        "ce:minilm6(gated(idf-v2+semantic))"}


def test_both_surfaces_gate_the_same_way(store, gate_ce, monkeypatch):
    _stub_both(monkeypatch)
    retrieval_io.configure(store, fusion=retrieval_io.FUSION_GATED,
                           rerank=retrieval_io.RERANK_CE_FAST)
    assert query_cmd.run(_args(store, TASK, experiment=True, occasion_id="cli-g")) == 0
    call(mcp_server.build_server(store), "retrieve", task=TASK, occasion_id="mcp-g")
    cli_rows, mcp_rows = _logged(store, "cli-g"), _logged(store, "mcp-g")
    assert set(cli_rows) == set(mcp_rows) and "refund-threshold" not in cli_rows
    for slug in cli_rows:
        for field in ("scorer", "relevance", "rank"):
            assert cli_rows[slug][field] == mcp_rows[slug][field], (slug, field)


def test_the_gate_and_the_label_follow_the_semantic_arms_model(store, gate_ce, monkeypatch):
    """With the arctic-embed index, the gate is -8 (rerank_arm.GATE_THRESHOLDS)
    and the label names the model: refund-threshold, semantic-only at -5, is
    kept off the original model's page (-4) and admitted to this one, on
    both surfaces, logged as a treatment of its own."""
    arctic = "Snowflake/snowflake-arctic-embed-m-v1.5"
    _stub_both(monkeypatch, model=arctic)
    fetched = []
    stub_mcp, stub_cli = semantic_arm.ranked_slugs, query_cmd._semantic_slugs
    monkeypatch.setattr(semantic_arm, "ranked_slugs", lambda root, q, top_k, agent_type=None: (
        fetched.append(("mcp", top_k)) or stub_mcp(root, q, top_k, agent_type)))
    monkeypatch.setattr(query_cmd, "_semantic_slugs", lambda args, root, hint, extra=0: (
        fetched.append(("cli", args.top_k + extra)) or stub_cli(args, root, hint, extra)))
    retrieval_io.configure(store, fusion=retrieval_io.FUSION_GATED,
                           rerank=retrieval_io.RERANK_CE)
    assert query_cmd.run(_args(store, TASK, experiment=True, occasion_id="cli-a")) == 0
    call(mcp_server.build_server(store), "retrieve", task=TASK, occasion_id="mcp-a")
    cli_rows, mcp_rows = _logged(store, "cli-a"), _logged(store, "mcp-a")
    assert set(cli_rows) == set(mcp_rows)
    assert "refund-threshold" in mcp_rows
    assert {r["scorer"] for r in list(cli_rows.values()) + list(mcp_rows.values())} == {
        "ce:minilm6(gated(idf-v2+semantic@arctic-m))"}
    # Both surfaces fetch the arctic rule's pool depth from the semantic arm.
    assert {depth for _surface, depth in fetched} == {10}
    assert {surface for surface, _depth in fetched} == {"cli", "mcp"}


def test_gated_fusion_without_a_reranker_is_lexical_and_says_so(store, monkeypatch):
    _stub_both(monkeypatch)
    retrieval_io.configure(store, fusion=retrieval_io.FUSION_GATED,
                           rerank=retrieval_io.RERANK_CE)
    monkeypatch.setattr(rerank_arm, "available", lambda: False)
    out = call(mcp_server.build_server(store), "retrieve", task=TASK, occasion_id="g-no")
    assert "reranker" in out["fusion_note"]
    assert "unsubscribe-sync" not in _slugs(out, "lessons", "withheld")
    assert {r["scorer"] for r in _logged(store, "g-no").values()} == {"idf-v2"}


def test_a_new_store_fuses_gated_by_default_where_both_models_are_installed(
    tmp_path, no_override, monkeypatch,
):
    from commontrace import semantic_arm

    monkeypatch.delenv(retrieval_io.DEFAULT_FUSION_ENV, raising=False)
    monkeypatch.setattr(rerank_arm, "available", lambda: True)
    monkeypatch.setattr(semantic_arm, "available", lambda: True)
    root = str(tmp_path)
    assert main(["init", "--dest", root]) == 0
    config = retrieval_io.load_config(root)
    assert (config.fusion, config.rerank) == (retrieval_io.FUSION_GATED, retrieval_io.RERANK_CE)
    assert config.eligibility == "ce:minilm6(gated(idf-v2+semantic))"
    monkeypatch.setenv(retrieval_io.DEFAULT_FUSION_ENV, "none")
    assert retrieval_io.load_config(root).fusion == retrieval_io.FUSION_NONE


def test_a_gated_store_is_pinned_to_gated_by_its_log(tmp_path, no_override, monkeypatch):
    monkeypatch.setattr(rerank_arm, "available", lambda: False)
    root = str(tmp_path)
    assert main(["init", "--dest", root]) == 0
    with open(holdout_io.holdout_log_path(root), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"occasion_id": "o", "lesson": "l", "injected": True, "rate": 0.5,
                             "salt": "s", "scorer": "ce:tinybert2(gated(idf-v2+semantic))",
                             "floor": 0.04}) + "\n")
    config = retrieval_io.load_config(root)
    assert (config.fusion, config.rerank) == (retrieval_io.FUSION_GATED, retrieval_io.RERANK_CE_FAST)


def test_an_empty_store_answers_without_loading_a_model_or_touching_the_index(tmp_path, monkeypatch, capsys):
    """Nothing to rank: both surfaces return the empty page at once instead
    of loading a cross-encoder and refreshing the semantic index (~15s on a
    fresh store), and neither says the reranker was skipped."""
    def _forbidden(*_a, **_k):
        raise AssertionError("an empty store must not load a model or build an index")

    monkeypatch.setattr(rerank_arm, "available", lambda: True)
    monkeypatch.setattr(rerank_arm, "_load", _forbidden)
    monkeypatch.setattr(semantic_arm, "available", lambda: True)
    monkeypatch.setattr(semantic_arm, "ensure_fresh", _forbidden)
    monkeypatch.setattr(query_cmd, "_refresh_stale_index", _forbidden)
    store = str(tmp_path / "fresh")
    assert main(["init", "--dest", store]) == 0
    retrieval_io.configure(store, fusion=retrieval_io.FUSION_GATED, rerank=retrieval_io.RERANK_CE)

    assert query_cmd.run(_args(store, TASK, top_k=2)) == 0
    assert "reranker" not in capsys.readouterr().err
    out = call(mcp_server.build_server(store), "retrieve", task=TASK, top_k=2)
    assert out["lessons"] == [] and not out.get("rerank_note") and not out.get("fusion_note")


def test_the_server_warms_the_models_a_store_will_use(store, monkeypatch):
    """The first `retrieve` should not wait seconds for a model the store
    was always going to load: `serve` loads them on a background thread."""
    loaded = []
    monkeypatch.setattr(rerank_arm, "available", lambda: True)
    monkeypatch.setattr(rerank_arm, "ready", lambda mode: loaded.append(("rerank", mode)) or "")
    monkeypatch.setattr(semantic_arm, "available", lambda: True)
    monkeypatch.setattr(semantic_arm, "ensure_fresh", lambda root: loaded.append(("index", root)) or "")
    monkeypatch.setattr(semantic_arm, "ranked_slugs", lambda *a, **k: loaded.append(("embed",)) or (0, [], []))
    retrieval_io.configure(store, fusion=retrieval_io.FUSION_GATED, rerank=retrieval_io.RERANK_CE)
    mcp_server._warm_models(store, delay=0)
    assert loaded == [("index", store), ("embed",), ("rerank", retrieval_io.RERANK_CE)]


def test_the_server_warms_nothing_a_store_will_not_use(tmp_path, monkeypatch):
    def _forbidden(*_a, **_k):
        raise AssertionError("nothing to warm")

    monkeypatch.setattr(rerank_arm, "ready", _forbidden)
    monkeypatch.setattr(semantic_arm, "ensure_fresh", _forbidden)
    empty = str(tmp_path / "empty")
    assert main(["init", "--dest", empty]) == 0
    retrieval_io.configure(empty, fusion=retrieval_io.FUSION_GATED, rerank=retrieval_io.RERANK_CE)
    mcp_server._warm_models(empty, delay=0)  # no lessons: nothing to rank

    lexical = str(tmp_path / "lexical")
    assert main(["init", "--dest", lexical]) == 0
    _write_lesson(lexical, "only", body="Do the thing.", description="the thing")
    retrieval_io.configure(lexical, fusion=retrieval_io.FUSION_NONE, rerank=retrieval_io.RERANK_NONE)
    mcp_server._warm_models(lexical, delay=0)  # lexical only: no model at all


def test_the_warm_up_can_be_turned_off(store, monkeypatch):
    started = []
    monkeypatch.setenv(mcp_server.WARM_ENV, "0")
    monkeypatch.setattr(mcp_server, "_warm_models", lambda *a, **k: started.append(a))
    import anyio

    monkeypatch.setattr(anyio, "run", lambda *a, **k: None)
    assert mcp_server.serve(store) == 0
    assert started == []


def test_the_cli_loads_the_reranker_alongside_the_semantic_arm(store, monkeypatch):
    """The reranker loads while the semantic arm runs, so the arm is asked
    for the reranking depth up front; when the model then loads, that one
    run is used."""
    _stub_both(monkeypatch)
    _rerank_store(store)
    depths = []
    stub = query_cmd._semantic_slugs
    monkeypatch.setattr(query_cmd, "_semantic_slugs", lambda args, root, hint, extra=0: (
        depths.append(args.top_k + extra) or stub(args, root, hint, extra)))
    monkeypatch.setattr(rerank_arm, "available", lambda: True)
    monkeypatch.setattr(rerank_arm, "ready", lambda mode: "")
    monkeypatch.setattr(rerank_arm, "rerank", lambda task, slugs, texts, top_k, **k: ([(s, 1.0) for s in slugs[:top_k]], []))
    assert query_cmd.run(_args(store, TASK, top_k=2)) == 0
    assert depths == [rerank_arm.pool_size(2, retrieval_io.RERANK_CE)]


def test_a_reranker_that_fails_to_load_mid_query_changes_nothing(store, monkeypatch):
    """If the model fails to load while the semantic arm ran at the deeper
    depth, the arm runs again at the plain depth: the result is exactly the
    store's result with reranking off."""
    _stub_both(monkeypatch)
    _rerank_store(store)
    monkeypatch.setattr(rerank_arm, "available", lambda: True)
    monkeypatch.setattr(rerank_arm, "ready", lambda mode: "the model could not load")
    retrieval_io.configure(store, fusion=retrieval_io.FUSION_RRF, rerank=retrieval_io.RERANK_CE)
    assert query_cmd.run(_args(store, TASK, top_k=2, experiment=True, occasion_id="failed-load")) == 0
    retrieval_io.configure(store, fusion=retrieval_io.FUSION_RRF, rerank=retrieval_io.RERANK_NONE)
    assert query_cmd.run(_args(store, TASK, top_k=2, experiment=True, occasion_id="plain")) == 0
    failed, plain = _logged(store, "failed-load"), _logged(store, "plain")
    assert failed.keys() == plain.keys() and failed
    assert {r["scorer"] for r in failed.values()} == {r["scorer"] for r in plain.values()}
