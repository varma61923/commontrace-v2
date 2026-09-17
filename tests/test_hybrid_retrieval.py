"""Both arms, fused -- and the experiment's denominator kept honest.

Until now `commontrace query` picked ONE retriever: semantic when the
attention extra was installed and the index was fresh, lexical otherwise.
Whichever it picked, the other arm's signal was discarded, so a store with
the extra could not find a lesson whose exact error string the user pasted
in, and a store without it could not find one phrased differently from the
task. They fail on different queries, which is exactly when fusing beats
picking.

What these tests defend, in order of how badly getting it wrong would hurt:

1. **Turning fusion on is visible to the drift check.** Fusion changes which
   lessons are eligible, and eligibility is the denominator of every causal
   number this product sells. An arm composition that changed mid-run without
   the log saying so would pool two treatments into one comparison --
   silently, and in the direction that flatters the product.
2. **A pinned store restores BOTH halves.** Reading a fused label back as a
   plain scorer would drop the semantic arm and change the denominator in
   exactly the direction the pinning exists to prevent.
3. **Fusion is by rank, not score.** Cosine and IDF relevance are not on a
   comparable scale, and there is no honest conversion between them.
4. **A failed semantic arm does not silently become a fused claim.** Serving
   the lexical half is fine; logging it under the fused label is not.
5. **Opting in is a decision, not an upgrade side effect.** A store that
   never asked for fusion must behave exactly as before.
"""
from __future__ import annotations

import argparse
import json
import os

import pytest

from commontrace import (
    frontmatter,
    holdout_io,
    lesson_io,
    paths,
    retrieval,
    retrieval_io,
)
from commontrace.commands import query_cmd

# --- the recorded label ------------------------------------------------------

class TestEligibilityLabel:
    def test_a_plain_scorer_round_trips(self):
        label = retrieval_io.eligibility_label("idf-v2", retrieval_io.FUSION_NONE)
        assert label == "idf-v2"
        assert retrieval_io.parse_eligibility_label(label) == (
            "idf-v2", retrieval_io.FUSION_NONE)

    def test_a_fused_label_round_trips(self):
        label = retrieval_io.eligibility_label("idf-v2", retrieval_io.FUSION_RRF)
        assert retrieval_io.parse_eligibility_label(label) == (
            "idf-v2", retrieval_io.FUSION_RRF)

    def test_the_two_labels_differ(self):
        """This is the whole mechanism: integrity.check_scorer_drift
        invalidates a run whose scorer label changed, so the fused and
        unfused configurations must not share one."""
        assert retrieval_io.eligibility_label("idf-v2", retrieval_io.FUSION_RRF) != (
            retrieval_io.eligibility_label("idf-v2", retrieval_io.FUSION_NONE))

    def test_a_pre_fusion_label_reads_as_no_fusion(self):
        """Every log line written before fusion existed carries a bare
        scorer, and must not be read as anything else."""
        assert retrieval_io.parse_eligibility_label("count") == (
            "count", retrieval_io.FUSION_NONE)
        assert retrieval_io.parse_eligibility_label("") == (
            "", retrieval_io.FUSION_NONE)

    def test_an_unrecognised_label_degrades_rather_than_raising(self):
        scorer, fusion = retrieval_io.parse_eligibility_label("rrf(weird")
        assert fusion == retrieval_io.FUSION_NONE
        assert scorer == "rrf(weird"


# --- config ------------------------------------------------------------------

def _write_config(root: str, **kwargs) -> None:
    path = retrieval_io.config_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(kwargs, fh)


class TestConfig:
    def test_fusion_is_off_unless_the_store_asked(self, tmp_path):
        """Opting in changes the eligibility denominator, so it must never
        arrive as an upgrade side effect."""
        assert retrieval_io.load_config(str(tmp_path)).fusion == retrieval_io.FUSION_NONE

    def test_fusion_is_read_from_the_store(self, tmp_path):
        _write_config(str(tmp_path), fusion="rrf")
        config = retrieval_io.load_config(str(tmp_path))
        assert config.fusion == retrieval_io.FUSION_RRF
        assert config.eligibility == "rrf(idf-v2+semantic)"

    def test_a_typo_does_not_stop_the_fleet_retrieving(self, tmp_path):
        """This file is read on every retrieval; a bad value must degrade,
        not raise."""
        _write_config(str(tmp_path), fusion="rff")
        assert retrieval_io.load_config(str(tmp_path)).fusion == retrieval_io.FUSION_NONE

    def test_rrf_k_is_configurable_and_positive(self, tmp_path):
        _write_config(str(tmp_path), rrf_k=10)
        assert retrieval_io.load_config(str(tmp_path)).rrf_k == 10
        _write_config(str(tmp_path), rrf_k=0)
        assert retrieval_io.load_config(str(tmp_path)).rrf_k >= 1

    def test_a_pinned_store_restores_both_halves(self, tmp_path):
        """Reading a fused label back as a plain scorer would drop the
        semantic arm and change the denominator in exactly the direction the
        pinning exists to prevent."""
        root = str(tmp_path)
        os.makedirs(paths.memory_dir(root), exist_ok=True)
        with open(holdout_io.holdout_log_path(root), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "lesson": "l", "occasion_id": "o", "injected": True,
                "rate": 0.5, "salt": "s",
                "scorer": "rrf(idf-v2+semantic)", "floor": 0.04,
            }) + "\n")
        config = retrieval_io.load_config(root)
        assert config.pinned_for_running_experiment
        assert config.scorer == "idf-v2"
        assert config.fusion == retrieval_io.FUSION_RRF
        # And it re-derives the same label it was pinned from, so a second
        # assignment cannot read as drift against the first.
        assert config.eligibility == "rrf(idf-v2+semantic)"

    def test_a_pinned_pre_fusion_store_stays_unfused(self, tmp_path):
        root = str(tmp_path)
        os.makedirs(paths.memory_dir(root), exist_ok=True)
        with open(holdout_io.holdout_log_path(root), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "lesson": "l", "occasion_id": "o", "injected": True,
                "rate": 0.5, "salt": "s", "scorer": "count", "floor": 0.0,
            }) + "\n")
        config = retrieval_io.load_config(root)
        assert config.fusion == retrieval_io.FUSION_NONE
        assert config.scorer == "count"

    def test_redundancy_threshold_is_off_unless_the_store_asked(self, tmp_path):
        """Suppressing an admitted lesson is a treatment change -- must never
        arrive as an upgrade side effect. Same posture as fusion."""
        from commontrace import dosage

        assert (
            retrieval_io.load_config(str(tmp_path)).redundancy_threshold
            == dosage.DEFAULT_REDUNDANCY_THRESHOLD
            == 0.0
        )

    def test_redundancy_threshold_is_read_from_the_store(self, tmp_path):
        _write_config(str(tmp_path), redundancy_threshold=0.4)
        assert retrieval_io.load_config(str(tmp_path)).redundancy_threshold == pytest.approx(0.4)

    def test_an_out_of_range_redundancy_threshold_degrades_rather_than_raising(self, tmp_path):
        """This file is read on every retrieval; a bad value must degrade,
        not raise -- same posture as the fusion typo case above."""
        from commontrace import dosage

        _write_config(str(tmp_path), redundancy_threshold=1.7)
        assert (
            retrieval_io.load_config(str(tmp_path)).redundancy_threshold
            == dosage.DEFAULT_REDUNDANCY_THRESHOLD
        )
        _write_config(str(tmp_path), redundancy_threshold=-0.5)
        assert (
            retrieval_io.load_config(str(tmp_path)).redundancy_threshold
            == dosage.DEFAULT_REDUNDANCY_THRESHOLD
        )

    def test_redundancy_threshold_does_not_change_eligibility(self, tmp_path):
        """Like the budget, this decides how many of the ELIGIBLE set are
        actually injected, not which lessons are eligible -- so it must not
        appear in the eligibility label the drift check keys on."""
        _write_config(str(tmp_path), redundancy_threshold=0.5)
        config = retrieval_io.load_config(str(tmp_path))
        assert config.eligibility == "idf-v2"


# --- the property the whole label design exists for ------------------------

class TestDriftIsCaught:
    """Turning fusion on mid-run must invalidate the experiment, through the
    EXISTING check rather than a new one. Carrying the arm composition inside
    the scorer label is what buys that: no second column an older reader
    would ignore, and no second check that could disagree with the first
    about the same fact."""

    def _rows(self, *labels):
        from commontrace import integrity

        return [
            integrity.Assignment(
                lesson="l", occasion_id=f"o{i}", injected=True, rate=0.5,
                salt="s", at=None, revision=None, relevance=0.5, rank=1,
                scorer=label, floor=0.04,
            )
            for i, label in enumerate(labels)
        ]

    def test_one_configuration_throughout_is_ok(self):
        from commontrace import integrity

        finding = integrity.check_scorer_drift(self._rows("idf-v2", "idf-v2"))
        assert finding.severity == integrity.SEVERITY_OK

    def test_turning_fusion_on_mid_run_invalidates_the_experiment(self):
        from commontrace import integrity

        finding = integrity.check_scorer_drift(self._rows(
            retrieval_io.eligibility_label("idf-v2", retrieval_io.FUSION_NONE),
            retrieval_io.eligibility_label("idf-v2", retrieval_io.FUSION_RRF),
        ))
        assert finding.severity == integrity.SEVERITY_INVALIDATES
        assert "rrf(idf-v2+semantic)" in finding.headline

    def test_a_consistently_fused_run_is_ok(self):
        """Fusion is not itself a problem -- CHANGING it mid-run is."""
        from commontrace import integrity

        label = retrieval_io.eligibility_label("idf-v2", retrieval_io.FUSION_RRF)
        finding = integrity.check_scorer_drift(self._rows(label, label))
        assert finding.severity == integrity.SEVERITY_OK


# --- settings survive being edited ------------------------------------------

class TestConfigureCarriesEverything:
    """`commontrace retrieval` writes the WHOLE settings file, not just the
    flags it was given."""

    def test_a_budget_survives_an_unrelated_edit(self, tmp_path):
        """The regression this exists for: writing only the named fields
        meant a store that had set a context budget lost it the next time
        anyone touched the floor. The budget silently reverted to the
        default, so an operator tightening precision by one flag also
        tripled how much text their agents received, with nothing printed."""
        root = str(tmp_path)
        retrieval_io.configure(root, max_lessons=3, max_chars=500)
        retrieval_io.configure(root, floor=0.1)
        config = retrieval_io.load_config(root)
        assert (config.max_lessons, config.max_chars) == (3, 500)
        assert config.floor == pytest.approx(0.1)

    def test_fusion_survives_an_unrelated_edit(self, tmp_path):
        root = str(tmp_path)
        retrieval_io.configure(root, fusion=retrieval_io.FUSION_RRF)
        retrieval_io.configure(root, floor=0.2)
        assert retrieval_io.load_config(root).fusion == retrieval_io.FUSION_RRF

    def test_a_note_is_not_erased_by_a_later_edit(self, tmp_path):
        root = str(tmp_path)
        retrieval_io.configure(root, floor=0.1, note="tuned for the pilot")
        retrieval_io.configure(root, scorer="idf-v2")
        assert retrieval_io.load_config(root).note == "tuned for the pilot"

    def test_an_unknown_fusion_mode_is_refused(self, tmp_path):
        """Unlike the read path, which degrades so a typo cannot stop a
        fleet retrieving, the WRITE path refuses: an operator who asked for
        something this build does not have must be told, not silently given
        the default."""
        with pytest.raises(ValueError, match="unknown fusion mode"):
            retrieval_io.configure(str(tmp_path), fusion="rff")

    def test_redundancy_threshold_survives_an_unrelated_edit(self, tmp_path):
        root = str(tmp_path)
        retrieval_io.configure(root, redundancy_threshold=0.4)
        retrieval_io.configure(root, floor=0.1)
        assert retrieval_io.load_config(root).redundancy_threshold == pytest.approx(0.4)

    def test_an_out_of_range_redundancy_threshold_is_refused_on_write(self, tmp_path):
        """The WRITE path refuses, matching the fusion case: an operator who
        set an invalid value must be told, not silently given the default."""
        with pytest.raises(ValueError, match="redundancy threshold"):
            retrieval_io.configure(str(tmp_path), redundancy_threshold=1.5)


# --- the fusion itself -------------------------------------------------------

class TestFusionIsByRank:
    def test_an_arm_only_lesson_still_places(self):
        """A lexical pass cannot be expected to find a paraphrase; treating
        its silence as a vote against would make adding an arm reduce
        recall."""
        fused = dict(retrieval.reciprocal_rank_fusion({
            "lexical": ["a", "b"],
            "semantic": ["c"],
        }))
        assert set(fused) == {"a", "b", "c"}

    def test_agreement_across_arms_outranks_one_arms_confidence(self):
        """The entire point of fusing: a lesson both arms liked beats one
        that only the first arm ranked top."""
        fused = retrieval.reciprocal_rank_fusion({
            "lexical": ["solo", "agreed"],
            "semantic": ["agreed", "other"],
        })
        assert fused[0][0] == "agreed"

    def test_scores_from_the_two_arms_are_never_compared(self):
        """Fusion reads positions only, so a lexical arm whose scores are on
        a wildly different scale changes nothing."""
        a = retrieval.reciprocal_rank_fusion({"x": ["p", "q"], "y": ["q", "p"]})
        b = retrieval.reciprocal_rank_fusion({"x": ["p", "q"], "y": ["q", "p"]})
        assert a == b


# --- the live path -----------------------------------------------------------

def _lesson(root: str, slug: str, description: str, body: str) -> None:
    path = os.path.join(paths.lessons_dir(root), f"lesson_{slug}.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lesson_io.write_lesson(
        path,
        {"name": slug, "description": description, "status": "active",
         "importance": 3, "tags": []},
        body, root=root, actor="test", reason="fixture",
    )
    assert frontmatter.read(path)


@pytest.fixture
def store(tmp_path):
    root = str(tmp_path / "fleet")
    os.makedirs(paths.lessons_dir(root), exist_ok=True)
    _lesson(root, "suppression-list",
            "Password reset email suppression failures.", "Check suppression.")
    _lesson(root, "refund-threshold",
            "Refunds above the manager approval threshold.", "Escalate.")
    return root


def _args(root: str, task: str, **extra) -> argparse.Namespace:
    base = dict(
        task=task, top_k=5, dest=root, agent_type=None, lexical=False,
        relevance_floor=None, include_importance_floor=None,
        experiment=False, occasion_id=None, holdout_rate=None,
        experiment_salt=None, exclude_shown=None,
    )
    base.update(extra)
    return argparse.Namespace(**base)


class TestHybridThroughTheCommand:
    """`_run_hybrid` is what the store's opt-in actually reaches. The
    semantic arm is stubbed because it is a subprocess needing
    sentence-transformers; everything else is the real path."""

    def _stub_semantic(self, monkeypatch, slugs, rc=0):
        """Stand in for the semantic arm, which is a subprocess needing
        sentence-transformers. The deps check and the index-freshness check
        are stubbed with it, because fusion legitimately requires both --
        without the extra there is no second arm to fuse, and the command
        correctly falls back before it ever reaches the fused path."""
        monkeypatch.setattr(query_cmd, "has_attention_deps", lambda: True)
        monkeypatch.setattr(query_cmd, "_index_is_unusable", lambda root: "")
        monkeypatch.setattr(
            query_cmd, "_semantic_slugs",
            lambda args, root, hint: (rc, list(slugs), ""),
        )

    def test_a_semantic_only_lesson_is_returned(self, store, monkeypatch, capsys):
        """The gap fusion closes: a lesson the lexical arm never surfaced."""
        _write_config(store, fusion="rrf")
        self._stub_semantic(monkeypatch, ["refund-threshold"])
        rc = query_cmd.run(_args(store, "password reset email suppression"))
        assert rc == 0
        out = capsys.readouterr().out
        assert "refund-threshold" in out
        assert "suppression-list" in out

    def test_the_output_names_which_arms_found_each_lesson(
        self, store, monkeypatch, capsys
    ):
        _write_config(store, fusion="rrf")
        self._stub_semantic(monkeypatch, ["refund-threshold"])
        query_cmd.run(_args(store, "password reset email suppression"))
        out = capsys.readouterr().out
        assert "arms: lexical" in out
        assert "semantic" in out

    def test_the_assignment_records_the_fused_label(
        self, store, monkeypatch, capsys
    ):
        """The property the drift check depends on."""
        _write_config(store, fusion="rrf")
        self._stub_semantic(monkeypatch, ["refund-threshold"])
        rc = query_cmd.run(_args(
            store, "password reset email suppression",
            experiment=True, occasion_id="occ-1", holdout_rate=0.5,
            experiment_salt="salt-1",
        ))
        assert rc == 0, capsys.readouterr()
        records, unreadable = holdout_io.read_log(store)
        assert unreadable == 0
        assert records
        assert {r.scorer for r in records} == {"rrf(idf-v2+semantic)"}

    def test_an_unfused_store_records_the_plain_scorer(
        self, store, monkeypatch, capsys
    ):
        """The two configurations must be distinguishable in the log, or
        check_scorer_drift cannot see a mid-run change."""
        monkeypatch.setattr(query_cmd, "has_attention_deps", lambda: False)
        rc = query_cmd.run(_args(
            store, "password reset email suppression",
            experiment=True, occasion_id="occ-2", holdout_rate=0.5,
            experiment_salt="salt-1",
        ))
        assert rc == 0, capsys.readouterr()
        records, _ = holdout_io.read_log(store)
        assert {r.scorer for r in records} == {"idf-v2"}

    def test_a_failed_semantic_arm_does_not_log_a_fused_claim(
        self, store, monkeypatch, capsys
    ):
        """Serving the lexical half is fine. Logging it under the fused
        label would claim an arm that did not run, and the two are different
        treatments."""
        _write_config(store, fusion="rrf")
        self._stub_semantic(monkeypatch, [], rc=1)
        rc = query_cmd.run(_args(
            store, "password reset email suppression",
            experiment=True, occasion_id="occ-3", holdout_rate=0.5,
            experiment_salt="salt-1",
        ))
        assert rc == 0, capsys.readouterr()
        records, _ = holdout_io.read_log(store)
        assert records
        assert {r.scorer for r in records} == {"idf-v2"}
        assert "must not be pooled" in capsys.readouterr().err

    def test_an_unfused_store_never_reaches_the_hybrid_path(
        self, store, monkeypatch
    ):
        """A store that never asked for fusion behaves exactly as before.

        The deps and the index are stubbed AVAILABLE on purpose: with them
        missing the command falls back before the fusion dispatch either
        way, so this would pass whether or not the opt-in were honoured.
        The only meaningful version of this test is the one where fusion
        COULD have run and did not.
        """
        called = []
        monkeypatch.setattr(query_cmd, "has_attention_deps", lambda: True)
        monkeypatch.setattr(query_cmd, "_index_is_unusable", lambda root: "")
        monkeypatch.setattr(
            query_cmd, "_run_hybrid",
            lambda *a, **k: called.append(1) or 0,
        )
        monkeypatch.setattr(
            query_cmd, "run_script",
            lambda *a, **k: (0, "suppression-list | cosine=0.9 | importance=3\n"),
        )
        query_cmd.run(_args(store, "password reset email suppression"))
        assert called == []

    def test_fusion_without_the_extra_falls_back_and_says_so(
        self, store, monkeypatch, capsys
    ):
        """A store can configure fusion on a machine that cannot run the
        semantic arm. It must fall back rather than fail -- and the operator
        has to learn that the treatment running is not the one configured,
        because the log will say so and their experiment depends on it."""
        _write_config(store, fusion="rrf")
        monkeypatch.setattr(query_cmd, "has_attention_deps", lambda: False)
        rc = query_cmd.run(_args(
            store, "password reset email suppression",
            experiment=True, occasion_id="occ-4", holdout_rate=0.5,
            experiment_salt="salt-1",
        ))
        assert rc == 0
        assert "attention" in capsys.readouterr().err
        records, _ = holdout_io.read_log(store)
        # The lexical label, because that is the arm that actually ran.
        assert {r.scorer for r in records} == {"idf-v2"}

    def test_a_query_matching_nothing_in_either_arm_says_so(
        self, store, monkeypatch, capsys
    ):
        _write_config(store, fusion="rrf")
        self._stub_semantic(monkeypatch, [])
        rc = query_cmd.run(_args(store, "zzz nothing matches this at all"))
        assert rc == 0
        out = capsys.readouterr().out
        assert "suppression-list" not in out


# --- the other surface -------------------------------------------------------

class TestMcpSurfaceIsHonestAboutFusion:
    """`commontrace query` and the MCP `retrieve` tool must not silently
    disagree about which lessons are eligible -- a disagreement is two
    treatments pooled into one experiment."""

    def test_the_mcp_surface_says_it_is_not_fusing(self, store):
        pytest.importorskip("mcp")
        import asyncio

        from commontrace import mcp_server

        _write_config(store, fusion="rrf")
        server = mcp_server.build_server(store)
        result = asyncio.run(server.call_tool(
            "retrieve", {"task": "password reset email suppression"}))
        payload = getattr(result, "structured_content", None)
        if payload:
            payload = payload.get("result", payload)
        else:
            payload = json.loads(result.content[0].text)
        assert "fusion_note" in payload
        # And it names what the log will actually say, which is the fact an
        # operator needs to reconcile the two surfaces.
        assert "idf-v2" in payload["fusion_note"]

    def test_an_unfused_store_gets_no_such_note(self, store):
        pytest.importorskip("mcp")
        import asyncio

        from commontrace import mcp_server

        server = mcp_server.build_server(store)
        result = asyncio.run(server.call_tool(
            "retrieve", {"task": "password reset email suppression"}))
        payload = getattr(result, "structured_content", None)
        if payload:
            payload = payload.get("result", payload)
        else:
            payload = json.loads(result.content[0].text)
        assert "fusion_note" not in payload
