"""Tests for commontrace/distill.py (the clustering logic) and the
`commontrace distill` / `lesson approve` / `lesson reject` CLI commands --
the generic Curator/Validator pipeline for any agent_type, not just the
code-review profile's Omega/Lambda subagents."""
import os

import pytest

from commontrace import distill, frontmatter
from commontrace.cli import main


def _trace(id, title, context_text, tags=None, agent_type="support"):
    return distill.TraceCandidate(
        id=id, path=f"/tmp/{id}.md", title=title, context_text=context_text,
        solution_text="fix", tags=tags or [], agent_type=agent_type,
    )


class TestFindClusters:
    def test_similar_traces_cluster_together(self):
        traces = [
            _trace("t1", "Refund confusion A", "customer confused about refund timeline contradictory docs"),
            _trace("t2", "Refund confusion B", "customer confused about refund timeline contradictory docs"),
            _trace("t3", "Unrelated password reset", "user forgot password needed reset link"),
        ]
        clusters = distill.find_clusters(traces, existing_lessons_source_traces=[])
        assert len(clusters) == 1
        assert {t.id for t in clusters[0].traces} == {"t1", "t2"}

    def test_below_min_cluster_size_is_dropped(self):
        traces = [
            _trace("t1", "Refund confusion A", "customer confused about refund timeline contradictory docs"),
            _trace("t2", "Totally different topic", "something about onboarding flows and tutorials"),
        ]
        clusters = distill.find_clusters(traces, existing_lessons_source_traces=[], min_cluster_size=2)
        assert clusters == []

    def test_already_curated_traces_are_excluded(self):
        traces = [
            _trace("t1", "Refund confusion A", "customer confused about refund timeline contradictory docs"),
            _trace("t2", "Refund confusion B", "customer confused about refund timeline contradictory docs"),
        ]
        # both already referenced by an existing lesson's source_traces
        clusters = distill.find_clusters(traces, existing_lessons_source_traces=[["t1", "t2"]])
        assert clusters == []

    def test_tag_overlap_lowers_effective_threshold(self):
        # weak-but-nonzero textual overlap (shares "widget"/"failed" out of otherwise
        # disjoint vocabulary) that alone falls short of 0.3, but clears 0.3*0.6=0.18
        # -- shared tags should let that lowered bar cluster them.
        traces = [
            _trace("t1", "Case one", "the widget assembly failed during calibration alpha", tags=["shared-tag"]),
            _trace("t2", "Case two", "widget failed unexpectedly on the second bravo run", tags=["shared-tag"]),
        ]
        clusters = distill.find_clusters(traces, existing_lessons_source_traces=[], similarity_threshold=0.3)
        assert len(clusters) == 1

    def test_tag_overlap_alone_with_zero_text_overlap_does_not_cluster(self):
        # tags can lower the bar, but must not substitute for it entirely --
        # two traces about genuinely unrelated content shouldn't cluster just
        # because someone tagged them the same.
        traces = [
            _trace("t1", "Case one", "alpha beta gamma delta", tags=["shared-tag"]),
            _trace("t2", "Case two", "epsilon zeta eta theta", tags=["shared-tag"]),
        ]
        clusters = distill.find_clusters(traces, existing_lessons_source_traces=[], similarity_threshold=0.3)
        assert clusters == []

    def test_larger_clusters_sort_first(self):
        traces = [
            _trace("a1", "pair one", "refund policy contradiction alpha"),
            _trace("a2", "pair two", "refund policy contradiction bravo"),
            _trace("b1", "trio one", "cuda kernel nondeterministic seed"),
            _trace("b2", "trio two", "cuda kernel nondeterministic result"),
            _trace("b3", "trio three", "cuda kernel nondeterministic output"),
        ]
        clusters = distill.find_clusters(traces, existing_lessons_source_traces=[])
        assert len(clusters) == 2
        assert len(clusters[0].traces) == 3
        assert len(clusters[1].traces) == 2


class TestProposalHelpers:
    def test_propose_domain_prefers_starter_domain_tag(self):
        cluster = distill.Cluster(traces=[_trace("t1", "x", "y", tags=["refunds", "other-tag"])])
        assert distill.propose_domain(cluster, "support") == "refunds"

    def test_propose_domain_falls_back_when_no_starter_tag_present(self):
        cluster = distill.Cluster(traces=[_trace("t1", "x", "y", tags=["nonstandard-tag"])])
        assert distill.propose_domain(cluster, "support") in distill.STARTER_DOMAINS["support"]

    def test_propose_tags_dedupes_preserving_order(self):
        cluster = distill.Cluster(
            traces=[_trace("t1", "x", "y", tags=["a", "b"]), _trace("t2", "x", "y", tags=["b", "c"])]
        )
        assert distill.propose_tags(cluster) == ["a", "b", "c"]


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMONTRACE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _capture(store, title, context, solution, tags="refunds,policy", agent_type="support"):
    rc = main(
        [
            "capture", "--title", title, "--context", context, "--solution", solution,
            "--tags", tags, "--agent-type", agent_type, "--dest", str(store),
        ]
    )
    assert rc == 0


class TestDistillCommand:
    def test_no_traces_is_a_clean_noop(self, store, capsys):
        main(["init", "--agent-type", "support", "--dest", str(store)])
        capsys.readouterr()
        assert main(["distill", "--dest", str(store)]) == 0
        assert "nothing to distill" in capsys.readouterr().out

    def test_writes_a_review_status_candidate_lesson_from_a_repeated_pattern(self, store, capsys):
        main(["init", "--agent-type", "support", "--dest", str(store)])
        for i in range(3):
            _capture(
                store,
                f"Refund confusion {i}",
                "customer confused about refund timeline contradictory docs escalated",
                "point to canonical refund policy page",
            )
        capsys.readouterr()
        assert main(["distill", "--dest", str(store)]) == 0

        lessons_dir = store / "memory" / "lessons"
        candidates = [f for f in os.listdir(lessons_dir) if f.startswith("lesson_candidate_")]
        assert len(candidates) == 1
        fm, body = frontmatter.read(str(lessons_dir / candidates[0]))
        assert fm["status"] == "review"
        assert len(fm["source_traces"]) == 3
        assert fm["agent_type"] == "support"
        assert "TODO" in body

    def test_written_candidate_passes_schema_validation(self, store, capsys):
        main(["init", "--agent-type", "code", "--dest", str(store)])
        for i in range(2):
            _capture(
                store,
                f"CUDA determinism issue {i}",
                "kernel produced nondeterministic results across runs on the same seed",
                "pin the RNG seed and disable nondeterministic cuDNN algorithms",
                tags="cuda-gpu",
                agent_type="code",
            )
        capsys.readouterr()
        main(["distill", "--dest", str(store)])
        capsys.readouterr()
        assert main(["lesson", "validate", "--dest", str(store)]) == 0

    def test_rerun_does_not_repropose_already_curated_traces(self, store, capsys):
        main(["init", "--agent-type", "support", "--dest", str(store)])
        for i in range(2):
            _capture(
                store, f"Refund confusion {i}",
                "customer confused about refund timeline contradictory docs", "fix",
            )
        capsys.readouterr()
        main(["distill", "--dest", str(store)])
        capsys.readouterr()
        assert main(["distill", "--dest", str(store)]) == 0
        assert "no repeated pattern found" in capsys.readouterr().out


class TestLessonApproveReject:
    def _make_review_lesson(self, store):
        main(["init", "--agent-type", "support", "--dest", str(store)])
        main(
            [
                "lesson", "new", "--slug", "lesson_candidate_test", "--description", "d",
                "--agent-type", "support", "--domain", "refunds", "--dest", str(store),
            ]
        )
        path = store / "memory" / "lessons" / "lesson_candidate_test.md"
        fm, body = frontmatter.read(str(path))
        fm["status"] = "review"
        frontmatter.write(str(path), fm, body)
        return path

    def test_approve_flips_status_to_active(self, store, capsys):
        path = self._make_review_lesson(store)
        capsys.readouterr()
        rc = main(["lesson", "approve", "lesson_candidate_test", "--rationale", "confirmed", "--dest", str(store)])
        assert rc == 0
        fm, body = frontmatter.read(str(path))
        assert fm["status"] == "active"
        assert "confirmed" in body

    def test_reject_flips_status_to_archived_and_requires_reason(self, store, capsys):
        path = self._make_review_lesson(store)
        capsys.readouterr()
        rc = main(["lesson", "reject", "lesson_candidate_test", "--reason", "not generalizable", "--dest", str(store)])
        assert rc == 0
        fm, body = frontmatter.read(str(path))
        assert fm["status"] == "archived"
        assert "not generalizable" in body

    def test_approve_refuses_a_lesson_not_in_review(self, store, capsys):
        main(["init", "--agent-type", "support", "--dest", str(store)])
        main(
            [
                "lesson", "new", "--slug", "lesson_already_active", "--description", "d",
                "--agent-type", "support", "--domain", "refunds", "--dest", str(store),
            ]
        )
        capsys.readouterr()
        rc = main(["lesson", "approve", "lesson_already_active", "--dest", str(store)])
        assert rc == 1
        assert "refusing to approve" in capsys.readouterr().err

    def test_approve_unknown_slug_fails_cleanly(self, store, capsys):
        main(["init", "--agent-type", "support", "--dest", str(store)])
        capsys.readouterr()
        rc = main(["lesson", "approve", "lesson_does_not_exist", "--dest", str(store)])
        assert rc == 1
        assert "no lesson found" in capsys.readouterr().err

    def test_approve_rejects_path_traversal_slug(self, store, capsys):
        main(["init", "--agent-type", "support", "--dest", str(store)])
        capsys.readouterr()
        rc = main(["lesson", "approve", "../../etc/passwd", "--dest", str(store)])
        assert rc == 1
