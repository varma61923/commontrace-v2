"""MCP `retrieve` runs the fused ranking a store configured, exactly as
`commontrace query` does.

Until this existed the MCP surface was lexical-only, because the semantic
arm was a subprocess that loads a model per call -- so a store that set
`fusion=rrf` gave its shell users the fused ranking and its agents the
weaker one (on LoCoMo, 54% vs 64.5% of answering turns in the top 10).
commontrace/semantic_arm.py now runs the same ranking
function in-process.

The semantic arm is stubbed here -- a sentence-transformer is not a test
dependency -- with the SAME fixed ranking on both surfaces, so what is
tested is everything around it: the freshness gate, fusion, the harm split,
exclude_shown, dosage, and above all that both surfaces make the same
eligibility decision and log it the same way.
"""
from __future__ import annotations

import json

import pytest

from commontrace import evidence, harm, holdout_io, integrity, retrieval_io, semantic_arm
from commontrace.commands import query_cmd
from tests.test_hybrid_retrieval import _args
from tests.test_mcp_server import _write_lesson, call, cli

pytest.importorskip("mcp", reason="`commontrace serve` needs the MCP SDK: pip install 'commontrace[serve]'")

from commontrace import mcp_server  # noqa: E402

TASK = "password reset email suppression"
SEMANTIC = ["refund-threshold", "suppression-list", "unsubscribe-sync"]


@pytest.fixture
def store(tmp_path):
    root = str(tmp_path / "fleet")
    assert cli("init", "--dest", root).returncode == 0
    _write_lesson(root, "suppression-list", body="Check the suppression list first.",
                  description="password reset email suppression failures")
    _write_lesson(root, "refund-threshold", body="Escalate refunds above the threshold.",
                  description="refunds above the manager approval threshold")
    _write_lesson(root, "unsubscribe-sync", body="Resync the unsubscribe list nightly.",
                  description="opt-out roster drifts from the mailing vendor")
    _write_lesson(root, "unrelated", body="Rotate keys.", description="rotate api keys quarterly")
    retrieval_io.configure(root, fusion=retrieval_io.FUSION_RRF)
    holdout_io.configure(root, rate=0.5)
    return root


def _stub_both(monkeypatch, slugs=SEMANTIC, rc=0):
    """The same fixed semantic ranking behind both surfaces."""
    monkeypatch.setattr(semantic_arm, "available", lambda: True)
    monkeypatch.setattr(semantic_arm, "ensure_fresh", lambda root: "")
    monkeypatch.setattr(query_cmd, "has_attention_deps", lambda: True)
    monkeypatch.setattr(query_cmd, "_index_is_unusable", lambda root: "")
    monkeypatch.setattr(
        semantic_arm, "ranked_slugs",
        lambda root, query, top_k, agent_type=None: (rc, list(slugs)[:top_k], []),
    )
    monkeypatch.setattr(
        query_cmd, "_semantic_slugs",
        lambda args, root, hint, extra=0: (rc, list(slugs)[: args.top_k + extra], ""),
    )


def _logged(root, occasion):
    with open(holdout_io.holdout_log_path(root), encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    return {r["lesson"]: r for r in rows if r["occasion_id"] == occasion}


def _slugs(out, *names):
    return {item["slug"] for name in names for item in out.get(name) or []}


def test_a_semantic_only_lesson_reaches_the_agent(store, monkeypatch):
    """The gap: `unsubscribe-sync` shares no word with the task; only the
    semantic arm finds it."""
    _stub_both(monkeypatch)
    out = call(mcp_server.build_server(store), "retrieve", task=TASK)
    assert "unsubscribe-sync" in _slugs(out, "lessons")
    assert "fusion_note" not in out


def test_both_surfaces_make_the_same_eligibility_decision(store, monkeypatch):
    """Same store, same query, same semantic ranking: the same lessons are
    eligible, logged under the same label with the same relevance and floor.
    (The arm each lands in differs per occasion id, by design.)"""
    _stub_both(monkeypatch)
    rc = query_cmd.run(_args(store, TASK, experiment=True, occasion_id="cli-1"))
    assert rc == 0
    call(mcp_server.build_server(store), "retrieve", task=TASK, occasion_id="mcp-1")

    cli_rows, mcp_rows = _logged(store, "cli-1"), _logged(store, "mcp-1")
    assert set(cli_rows) == set(mcp_rows) and cli_rows
    for slug in cli_rows:
        for field in ("scorer", "floor", "relevance"):
            assert cli_rows[slug][field] == mcp_rows[slug][field], (slug, field)
    assert {r["scorer"] for r in mcp_rows.values()} == {"rrf(idf-v2+semantic)"}


def test_the_receipt_records_the_fused_label(store, monkeypatch):
    from commontrace import receipts

    _stub_both(monkeypatch)
    call(mcp_server.build_server(store), "retrieve", task=TASK, occasion_id="rcpt-1")
    [receipt] = [r for r in receipts.read_all(store) if r.occasion_id == "rcpt-1"]
    assert receipt.scorer == "rrf(idf-v2+semantic)"


@pytest.mark.parametrize("why,setup", [
    ("attention extra", lambda mp: mp.setattr(semantic_arm, "available", lambda: False)),
    ("index", lambda mp: mp.setattr(semantic_arm, "ensure_fresh",
                                    lambda root: "no semantic index has been built yet")),
    ("failed", lambda mp: mp.setattr(semantic_arm, "ranked_slugs",
                                     lambda *a, **k: (1, [], ["corrupt index"]))),
])
def test_when_fusion_cannot_run_it_falls_back_to_lexical_and_says_why(
    store, monkeypatch, why, setup,
):
    _stub_both(monkeypatch)
    setup(monkeypatch)
    out = call(mcp_server.build_server(store), "retrieve", task=TASK, occasion_id="fb-1")
    assert "unsubscribe-sync" not in _slugs(out, "lessons", "withheld")
    assert why in out["fusion_note"]
    assert {r["scorer"] for r in _logged(store, "fb-1").values()} == {"idf-v2"}


def test_exclude_shown_applies_to_the_semantic_arm_too(store, monkeypatch):
    _stub_both(monkeypatch)
    server = mcp_server.build_server(store)
    first = call(server, "retrieve", task=TASK, occasion_id="long-task")
    shown = _slugs(first, "lessons") - {item["slug"] for item in first.get("withheld") or []}
    second = call(server, "retrieve", task=TASK, occasion_id="long-task-2",
                  exclude_shown="long-task")
    assert not (_slugs(second, "lessons", "withheld") & shown)


def test_a_withdrawn_lesson_is_kept_out_of_the_semantic_arm_too(store, monkeypatch):
    """Harm withdrawal (commontrace/harm.py) must hold on both arms, or the
    lesson comes back through the one that was not filtered."""
    _stub_both(monkeypatch)
    from commontrace import evidence

    monkeypatch.setattr(evidence, "withdrawn", lambda root, policy: {
        "unsubscribe-sync": {"verdict": "HURTS", "effect": -0.3},
    })
    out = call(mcp_server.build_server(store), "retrieve", task=TASK)
    assert "unsubscribe-sync" not in _slugs(out, "lessons", "withheld")
    assert "unsubscribe-sync" in {w["slug"] for w in out["withdrawn"]}
    assert all(w["reason"] == harm.REASON for w in out["withdrawn"])


def test_a_store_without_fusion_never_touches_the_semantic_arm(tmp_path, monkeypatch):
    root = str(tmp_path / "plain")
    assert cli("init", "--dest", root).returncode == 0
    _write_lesson(root, "suppression-list", body="x", description="password reset email suppression")

    def boom(*a, **k):
        raise AssertionError("semantic arm called without fusion configured")

    monkeypatch.setattr(semantic_arm, "ranked_slugs", boom)
    out = call(mcp_server.build_server(root), "retrieve", task=TASK)
    assert _slugs(out, "lessons") == {"suppression-list"}


def test_exclude_shown_leaves_both_surfaces_logging_the_same_relevance(store, monkeypatch):
    """Filtering the semantic arm's output is not only about which lessons
    come back -- a shown lesson is dropped either way -- it decides the
    arm's RANK POSITIONS, and so every fused score written to the holdout
    log. Both surfaces must filter it the same way."""
    _stub_both(monkeypatch)
    config = holdout_io.load_config(store)
    holdout_io.assign_and_log(store, ["refund-threshold"], occasion_id="seed",
                              rate=0.0, salt=config.salt)
    rc = query_cmd.run(_args(store, TASK, experiment=True, occasion_id="cli-x",
                             exclude_shown="seed"))
    assert rc == 0
    call(mcp_server.build_server(store), "retrieve", task=TASK, occasion_id="mcp-x",
         exclude_shown="seed")
    cli_rows, mcp_rows = _logged(store, "cli-x"), _logged(store, "mcp-x")
    assert "refund-threshold" not in cli_rows and cli_rows
    assert {s: r["relevance"] for s, r in cli_rows.items()} == {
        s: r["relevance"] for s, r in mcp_rows.items()}


def test_both_surfaces_refresh_the_index_before_the_arm_ranks(store, monkeypatch):
    """A lesson approved since the last build must reach the semantic arm
    without anyone running `commontrace index`: each surface refreshes a
    stale index first (tests/test_semantic_arm.py covers the refresh itself)."""
    _stub_both(monkeypatch)
    events = []

    def refresh(root):
        events.append("refresh")
        return ""

    def rank(root, query, top_k, agent_type=None):
        events.append("rank")
        return 0, SEMANTIC[:top_k], []

    monkeypatch.setattr(semantic_arm, "ensure_fresh", refresh)
    monkeypatch.setattr(semantic_arm, "ranked_slugs", rank)
    call(mcp_server.build_server(store), "retrieve", task=TASK)
    assert events == ["refresh", "rank"]

    events.clear()
    monkeypatch.setattr(query_cmd, "_refresh_stale_index", refresh)
    monkeypatch.setattr(query_cmd, "_semantic_slugs",
                        lambda args, root, hint, extra=0: rank(root, "", args.top_k + extra)[:2] + ("",))
    assert query_cmd.run(_args(store, TASK)) == 0
    assert events == ["refresh", "rank"]


def test_a_fused_experiment_is_not_called_marginal(store, monkeypatch):
    """Fused rows record a rank-fusion score beside the lexical floor; the
    audit used to call all of them marginal (tests/test_integrity_fusion.py)."""
    _stub_both(monkeypatch)
    server = mcp_server.build_server(store)
    for i in range(40):
        call(server, "retrieve", task=TASK, occasion_id=f"o{i}")
    rows = evidence.analyse(store).rows
    assert rows and {r.scorer for r in rows} == {"rrf(idf-v2+semantic)"}
    finding = integrity.check_marginal_eligibility(rows)
    assert finding.severity == integrity.SEVERITY_OK
    assert finding.numbers["n_not_floor_gated"] == len(rows)
