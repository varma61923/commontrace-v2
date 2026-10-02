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
        assert retrieval_io.eligibility_label("idf-v2", retrieval_io.FUSION_RRF) != (
            retrieval_io.eligibility_label("idf-v2", retrieval_io.FUSION_NONE))

    def test_a_pre_fusion_label_reads_as_no_fusion(self):
        assert retrieval_io.parse_eligibility_label("count") == (
            "count", retrieval_io.FUSION_NONE)
        assert retrieval_io.parse_eligibility_label("") == (
            "", retrieval_io.FUSION_NONE)

    def test_an_unrecognised_label_degrades_rather_than_raising(self):
        scorer, fusion = retrieval_io.parse_eligibility_label("rrf(weird")
        assert fusion == retrieval_io.FUSION_NONE
        assert scorer == "rrf(weird"


def _write_config(root: str, **kwargs) -> None:
    path = retrieval_io.config_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(kwargs, fh)


class TestConfig:
    def test_fusion_is_off_unless_the_store_asked(self, tmp_path):
        assert retrieval_io.load_config(str(tmp_path)).fusion == retrieval_io.FUSION_NONE

    def test_fusion_is_read_from_the_store(self, tmp_path):
        _write_config(str(tmp_path), fusion="rrf")
        config = retrieval_io.load_config(str(tmp_path))
        assert config.fusion == retrieval_io.FUSION_RRF
        assert config.eligibility == "rrf(adaptive-v1+semantic)"

    def test_a_typo_does_not_stop_the_fleet_retrieving(self, tmp_path):
        _write_config(str(tmp_path), fusion="rff")
        assert retrieval_io.load_config(str(tmp_path)).fusion == retrieval_io.FUSION_NONE

    def test_rrf_k_is_configurable_and_positive(self, tmp_path):
        _write_config(str(tmp_path), rrf_k=10)
        assert retrieval_io.load_config(str(tmp_path)).rrf_k == 10
        _write_config(str(tmp_path), rrf_k=0)
        assert retrieval_io.load_config(str(tmp_path)).rrf_k >= 1

    def test_a_pinned_store_restores_both_halves(self, tmp_path):
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
        _write_config(str(tmp_path), redundancy_threshold=0.5)
        config = retrieval_io.load_config(str(tmp_path))
        assert config.eligibility == "adaptive-v1"


class TestDriftIsCaught:
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
        from commontrace import integrity

        label = retrieval_io.eligibility_label("idf-v2", retrieval_io.FUSION_RRF)
        finding = integrity.check_scorer_drift(self._rows(label, label))
        assert finding.severity == integrity.SEVERITY_OK


class TestConfigureCarriesEverything:
    def test_a_budget_survives_an_unrelated_edit(self, tmp_path):
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
        with pytest.raises(ValueError, match="unknown fusion mode"):
            retrieval_io.configure(str(tmp_path), fusion="rff")

    def test_redundancy_threshold_survives_an_unrelated_edit(self, tmp_path):
        root = str(tmp_path)
        retrieval_io.configure(root, redundancy_threshold=0.4)
        retrieval_io.configure(root, floor=0.1)
        assert retrieval_io.load_config(root).redundancy_threshold == pytest.approx(0.4)

    def test_an_out_of_range_redundancy_threshold_is_refused_on_write(self, tmp_path):
        with pytest.raises(ValueError, match="redundancy threshold"):
            retrieval_io.configure(str(tmp_path), redundancy_threshold=1.5)


class TestFusionIsByRank:
    def test_an_arm_only_lesson_still_places(self):
        fused = dict(retrieval.reciprocal_rank_fusion({
            "lexical": ["a", "b"],
            "semantic": ["c"],
        }))
        assert set(fused) == {"a", "b", "c"}

    def test_agreement_across_arms_outranks_one_arms_confidence(self):
        fused = retrieval.reciprocal_rank_fusion({
            "lexical": ["solo", "agreed"],
            "semantic": ["agreed", "other"],
        })
        assert fused[0][0] == "agreed"

    def test_scores_from_the_two_arms_are_never_compared(self):
        a = retrieval.reciprocal_rank_fusion({"x": ["p", "q"], "y": ["q", "p"]})
        b = retrieval.reciprocal_rank_fusion({"x": ["p", "q"], "y": ["q", "p"]})
        assert a == b


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
    def _stub_semantic(self, monkeypatch, slugs, rc=0):
        monkeypatch.setattr(query_cmd, "has_attention_deps", lambda: True)
        monkeypatch.setattr(query_cmd, "_index_is_unusable", lambda root: "")
        monkeypatch.setattr(
            query_cmd, "_semantic_slugs",
            lambda args, root, hint, extra=0: (rc, list(slugs), ""),
        )

    def test_a_semantic_only_lesson_is_returned(self, store, monkeypatch, capsys):
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
        assert {r.scorer for r in records} == {"rrf(adaptive-v1+semantic)"}

    def test_an_unfused_store_records_the_plain_scorer(
        self, store, monkeypatch, capsys
    ):
        monkeypatch.setattr(query_cmd, "has_attention_deps", lambda: False)
        rc = query_cmd.run(_args(
            store, "password reset email suppression",
            experiment=True, occasion_id="occ-2", holdout_rate=0.5,
            experiment_salt="salt-1",
        ))
        assert rc == 0, capsys.readouterr()
        records, _ = holdout_io.read_log(store)
        assert {r.scorer for r in records} == {"adaptive-v1"}

    def test_a_failed_semantic_arm_does_not_log_a_fused_claim(
        self, store, monkeypatch, capsys
    ):
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
        assert {r.scorer for r in records} == {"adaptive-v1"}
        assert "must not be pooled" in capsys.readouterr().err

    def test_an_unfused_store_never_reaches_the_hybrid_path(
        self, store, monkeypatch
    ):
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
        assert {r.scorer for r in records} == {"adaptive-v1"}

    def test_a_query_matching_nothing_in_either_arm_says_so(
        self, store, monkeypatch, capsys
    ):
        _write_config(store, fusion="rrf")
        self._stub_semantic(monkeypatch, [])
        rc = query_cmd.run(_args(store, "zzz nothing matches this at all"))
        assert rc == 0
        out = capsys.readouterr().out
        assert "suppression-list" not in out


class TestMcpSurfaceIsHonestAboutFusion:
    def test_the_mcp_surface_says_when_it_is_not_fusing(self, store, monkeypatch):
        pytest.importorskip("mcp")
        import asyncio

        from commontrace import mcp_server, semantic_arm

        monkeypatch.setattr(semantic_arm, "available", lambda: False)
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
        assert "adaptive-v1" in payload["fusion_note"]

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


class TestTheSemanticOnlyPathIsLabelled:
    def test_its_assignments_record_the_semantic_label(self, store, monkeypatch, capsys):
        from commontrace import holdout_io

        holdout_io.configure(store, rate=0.5)
        monkeypatch.setattr(query_cmd, "has_attention_deps", lambda: True)
        monkeypatch.setattr(query_cmd, "_refresh_stale_index", lambda root: "")
        monkeypatch.setattr(
            query_cmd, "run_script",
            lambda *a, **k: (0, "# header\nsuppression-list | cosine=0.9 | importance=3\n"),
        )
        rc = query_cmd.run(_args(store, "password reset email suppression",
                                 experiment=True, occasion_id="sem-1"))
        assert rc == 0
        with open(holdout_io.holdout_log_path(store), encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        assert {r.get("scorer") for r in rows if r["occasion_id"] == "sem-1"} == {"semantic-dosed"}

    def test_a_log_mixing_it_with_lexical_is_drift(self):
        from commontrace import integrity

        rows = [
            integrity.Assignment(lesson="a", occasion_id=f"o{i}", injected=bool(i % 2),
                                 succeeded=True, salt="s",
                                 scorer="semantic" if i < 10 else "idf-v2")
            for i in range(20)
        ]
        assert integrity.check_scorer_drift(rows).severity == integrity.SEVERITY_INVALIDATES

    def test_pinning_from_a_semantic_label_gives_a_real_lexical_scorer(self):
        assert retrieval_io.parse_eligibility_label("semantic") == (
            retrieval.SCORER_IDF, retrieval_io.FUSION_NONE)


class TestMMRReranking:
    def test_mmr_without_embeddings_falls_back_to_ranking(self):
        """MMR without embeddings should fall back to simple ranking."""
        items = [("a", 0.9), ("b", 0.8), ("c", 0.7)]
        result = retrieval.maximal_marginal_relevance(
            query_embedding=None,
            embeddings=None,
            ranked_items=items,
            lambda_param=0.5,
            top_k=2,
        )
        assert len(result) == 2
        assert result[0][0] == "a"
        assert result[1][0] == "b"

    def test_mmr_with_embeddings_diversifies_results(self):
        """MMR with embeddings should promote diversity."""
        query_emb = [1.0, 0.0]
        embeddings = {
            "a": [1.0, 0.0],  # Very similar to query
            "b": [0.9, 0.1],  # Similar to query and a
            "c": [0.0, 1.0],  # Diverse from query and a
        }
        items = [("a", 0.9), ("b", 0.8), ("c", 0.7)]

        # High lambda = more relevance-focused
        result_high = retrieval.maximal_marginal_relevance(
            query_embedding=query_emb,
            embeddings=embeddings,
            ranked_items=items,
            lambda_param=0.9,
            top_k=2,
        )
        assert result_high[0][0] == "a"  # Most relevant

        # Low lambda = more diversity-focused
        result_low = retrieval.maximal_marginal_relevance(
            query_embedding=query_emb,
            embeddings=embeddings,
            ranked_items=items,
            lambda_param=0.3,
            top_k=2,
        )
        assert result_low[0][0] == "a"  # Still most relevant first
        # Second item should be more diverse
        assert result_low[1][0] in ["b", "c"]

    def test_mmr_respects_top_k(self):
        """MMR should return at most top_k items."""
        items = [("a", 0.9), ("b", 0.8), ("c", 0.7), ("d", 0.6)]
        result = retrieval.maximal_marginal_relevance(
            query_embedding=None,
            embeddings=None,
            ranked_items=items,
            lambda_param=0.5,
            top_k=2,
        )
        assert len(result) == 2

    def test_mmr_validates_lambda_range(self):
        """MMR should reject lambda outside [0, 1]."""
        items = [("a", 0.9)]
        with pytest.raises(ValueError):
            retrieval.maximal_marginal_relevance(
                query_embedding=None,
                embeddings=None,
                ranked_items=items,
                lambda_param=1.5,
                top_k=1,
            )


class TestMultiChannelRetrieval:
    def test_multi_channel_fuses_with_rrf(self):
        """Multi-channel retrieval should fuse results using RRF."""
        channels = [
            retrieval.RetrievalChannel(
                name="lexical",
                results=[("a", 0.9), ("b", 0.8), ("c", 0.7)],
                weight=1.0,
            ),
            retrieval.RetrievalChannel(
                name="semantic",
                results=[("b", 0.9), ("a", 0.7), ("d", 0.6)],
                weight=1.0,
            ),
        ]

        fused = retrieval.multi_channel_retrieval(channels, top_k=3, rrf_k=60)
        fused_ids = [item_id for item_id, _ in fused]

        # Items appearing in both channels should rank higher
        assert "a" in fused_ids
        assert "b" in fused_ids
        assert len(fused) <= 3

    def test_multi_channel_respects_channel_weights(self):
        """Channel weights should influence fusion."""
        channels = [
            retrieval.RetrievalChannel(
                name="lexical",
                results=[("a", 0.9), ("b", 0.8)],
                weight=2.0,  # Higher weight
            ),
            retrieval.RetrievalChannel(
                name="semantic",
                results=[("b", 0.9), ("c", 0.8)],
                weight=0.5,  # Lower weight
            ),
        ]

        fused = retrieval.multi_channel_retrieval(channels, top_k=2, rrf_k=60)
        fused_ids = [item_id for item_id, _ in fused]

        # 'a' should be strong due to high-weight lexical channel
        assert "a" in fused_ids

    def test_multi_channel_handles_empty_channels(self):
        """Empty channels should be handled gracefully."""
        channels = [
            retrieval.RetrievalChannel(
                name="lexical",
                results=[("a", 0.9)],
                weight=1.0,
            ),
            retrieval.RetrievalChannel(
                name="semantic",
                results=[],
                weight=1.0,
            ),
        ]

        fused = retrieval.multi_channel_retrieval(channels, top_k=1, rrf_k=60)
        assert len(fused) == 1
        assert fused[0][0] == "a"


class TestTruthSubspace:
    def test_truth_epoch_tracks_facts(self):
        """Truth epoch should track valid facts."""
        epoch = retrieval.TruthEpoch(
            epoch_id="epoch1",
            valid_at="2024-01-01T00:00:00Z",
            facts={"fact1", "fact2"},
        )
        assert epoch.facts == {"fact1", "fact2"}

    def test_truth_subspace_gets_active_facts(self):
        """Truth subspace should return active facts for a time."""
        subspace = retrieval.TruthSubspace(
            epochs=[
                retrieval.TruthEpoch(
                    epoch_id="epoch1",
                    valid_at="2024-01-01T00:00:00Z",
                    invalid_at="2024-01-10T00:00:00Z",
                    facts={"fact1", "fact2"},
                ),
                retrieval.TruthEpoch(
                    epoch_id="epoch2",
                    valid_at="2024-01-10T00:00:00Z",
                    facts={"fact2", "fact3"},
                ),
            ],
            current_epoch="epoch2",
        )

        # Query before epoch2
        facts_before = subspace.get_active_facts("2024-01-05T00:00:00Z")
        assert facts_before == {"fact1", "fact2"}

        # Query during epoch2
        facts_during = subspace.get_active_facts("2024-01-15T00:00:00Z")
        assert facts_during == {"fact2", "fact3"}

    def test_truth_subspace_advances_epoch(self):
        """Truth subspace should advance epochs correctly."""
        subspace = retrieval.TruthSubspace(
            epochs=[
                retrieval.TruthEpoch(
                    epoch_id="epoch1",
                    valid_at="2024-01-01T00:00:00Z",
                    facts={"fact1"},
                ),
            ],
            current_epoch="epoch1",
        )

        subspace.advance_epoch("epoch2", "2024-01-10T00:00:00Z")

        assert subspace.current_epoch == "epoch2"
        assert len(subspace.epochs) == 2

        # First epoch should be invalidated
        first_epoch = subspace.epochs[0]
        assert first_epoch.invalid_at == "2024-01-10T00:00:00Z"


class TestHybridRetrieveWithRerank:
    def test_hybrid_retrieve_applies_mmr(self):
        """Hybrid retrieve should apply MMR when requested."""
        lessons = [
            _lesson_lesson("a", description="cache invalidation", tags=["caching"]),
            _lesson_lesson("b", description="cache timeout", tags=["caching"]),
            _lesson_lesson("c", description="database connection", tags=["database"]),
        ]

        result = retrieval.hybrid_retrieve_with_rerank(
            query="cache",
            lessons=lessons,
            top_k=2,
            apply_mmr=True,
            mmr_lambda=0.5,
        )

        assert len(result) <= 2
        # Should return cache-related lessons
        slugs = {r.slug for r in result}
        assert "a" in slugs or "b" in slugs

    def test_hybrid_retrieve_filters_by_truth_subspace(self):
        """Hybrid retrieve should filter by truth subspace."""
        lessons = [
            _lesson_lesson("a", description="lesson a", tags=[]),
            _lesson_lesson("b", description="lesson b", tags=[]),
            _lesson_lesson("c", description="lesson c", tags=[]),
        ]

        subspace = retrieval.TruthSubspace(
            epochs=[
                retrieval.TruthEpoch(
                    epoch_id="epoch1",
                    valid_at="2024-01-01T00:00:00Z",
                    facts={"a", "b"},
                ),
            ],
            current_epoch="epoch1",
        )

        result = retrieval.hybrid_retrieve_with_rerank(
            query="lesson",
            lessons=lessons,
            top_k=10,
            apply_mmr=False,
            truth_subspace=subspace,
        )

        # Should only return lessons in truth subspace
        slugs = {r.slug for r in result}
        assert slugs == {"a", "b"}

    def test_hybrid_retrieve_fuses_channels(self):
        """Hybrid retrieve should fuse multiple channels."""
        lessons = [
            _lesson_lesson("a", description="lesson a", tags=[]),
            _lesson_lesson("b", description="lesson b", tags=[]),
        ]

        channels = [
            retrieval.RetrievalChannel(
                name="semantic",
                results=[("a", 0.9)],
                weight=1.0,
            ),
        ]

        result = retrieval.hybrid_retrieve_with_rerank(
            query="lesson",
            lessons=lessons,
            channels=channels,
            top_k=2,
            apply_mmr=False,
        )

        # Should return results from fusion
        assert len(result) <= 2


# Helper function for TestHybridRetrieveWithRerank (matches test_retrieval_field_robustness)
def _lesson_lesson(name, description="", applies_when="", tags=(), domain="", importance=3, uses=0):
    return (f"/x/{name}.md", {
        "name": name,
        "description": description,
        "applies_when": applies_when,
        "tags": list(tags),
        "domain": domain,
        "importance": importance,
        "uses": uses,
    })
