"""Tests for commontrace/distill.py (the clustering logic) and the
`commontrace distill` / `lesson approve` / `lesson reject` CLI commands --
the generic Curator/Validator pipeline for any agent_type, not just the
code-review profile's Omega/Lambda subagents."""
import os

import pytest

from commontrace import distill, frontmatter
from commontrace.cli import main
from commontrace.commands import distill_cmd


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


    def test_disjoint_vocabulary_traces_never_cluster(self):
        # Exercises the inverted-index pre-filter path directly: traces
        # that share zero tokens must still correctly not cluster.
        traces = [
            _trace("t1", "alpha", "alpha beta gamma delta"),
            _trace("t2", "epsilon", "epsilon zeta eta theta"),
        ]
        clusters = distill.find_clusters(traces, existing_lessons_source_traces=[])
        assert clusters == []

    def test_zero_threshold_clusters_everything_regardless_of_overlap(self):
        traces = [
            _trace("t1", "alpha", "alpha beta gamma delta"),
            _trace("t2", "epsilon", "epsilon zeta eta theta"),
        ]
        clusters = distill.find_clusters(
            traces, existing_lessons_source_traces=[], similarity_threshold=0
        )
        assert len(clusters) == 1
        assert {t.id for t in clusters[0].traces} == {"t1", "t2"}


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

    def test_a_degenerate_similarity_threshold_is_rejected_not_run(self, store, capsys):
        """similarity<=0 makes find_clusters merge the whole store into one
        cluster (a deliberate library behavior other tests exercise
        directly) and then makes the O(k^2) medoid search after it run over
        that single giant cluster. Rejected at the CLI argument-parsing
        boundary -- the same value passed to the MCP `propose_lessons` tool
        is rejected for the identical reason, since both route through this
        parser."""
        main(["init", "--agent-type", "support", "--dest", str(store)])
        capsys.readouterr()
        with pytest.raises(SystemExit) as exc:
            main(["distill", "--dest", str(store), "--similarity-threshold", "0"])
        assert exc.value.code != 0
        assert "similarity" in capsys.readouterr().err.lower()

    def test_a_similarity_threshold_above_one_is_rejected(self, store, capsys):
        main(["init", "--agent-type", "support", "--dest", str(store)])
        capsys.readouterr()
        with pytest.raises(SystemExit) as exc:
            main(["distill", "--dest", str(store), "--similarity-threshold", "1.5"])
        assert exc.value.code != 0
        assert "similarity" in capsys.readouterr().err.lower()

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

    def test_a_corrupt_trace_file_is_skipped_with_a_warning_not_a_crash(self, store, capsys):
        """Regression test for a real bug: trace files are explicitly meant
        to be readable/hand-editable, but _load_traces called
        trace_io.read() with nothing catching the FrontmatterError it can
        raise on malformed YAML -- one bad file crashed `distill` for the
        whole store instead of being skipped like measure_performance
        already does for the same kind of malformed input."""
        main(["init", "--agent-type", "support", "--dest", str(store)])
        for i in range(3):
            _capture(
                store,
                f"Refund confusion {i}",
                "customer confused about refund timeline contradictory docs escalated",
                "point to canonical refund policy page",
            )
        traces_dir = store / "memory" / "traces"
        (traces_dir / "zzz_corrupt.md").write_text(
            "---\nid: [unclosed\n---\n\n## Context\nc\n\n## Solution\ns\n", encoding="utf-8"
        )
        capsys.readouterr()
        assert main(["distill", "--dest", str(store)]) == 0
        err = capsys.readouterr().err
        assert "skipping unreadable trace" in err
        assert "zzz_corrupt.md" in err

    def test_a_hand_edited_scalar_tags_field_does_not_split_into_characters(self, store, capsys):
        """Regression test: `_load_traces` used
        `tags=list(instance.get("tags") or [])`. A hand-edited trace with
        `tags: auth,billing` (no YAML list brackets) parses as the plain
        string "auth,billing", and `list("auth,billing")` iterates it
        character by character -- ['a', 'u', 't', 'h', ',', ...] -- instead
        of raising or producing the two intended tags. Same malformed-input
        class overlap_cmd.py's _safe_tags already guards against."""
        main(["init", "--agent-type", "support", "--dest", str(store)])
        _capture(
            store, "Refund confusion 0",
            "customer confused about refund timeline contradictory docs escalated",
            "point to canonical refund policy page",
        )
        traces_dir = store / "memory" / "traces"
        trace_path = next(p for p in traces_dir.glob("*.md") if p.name != "README.md")
        fm, body = frontmatter.read(str(trace_path))
        fm["tags"] = "auth,billing"  # scalar, not a YAML list -- the malformed shape
        frontmatter.write(str(trace_path), fm, body)

        traces = distill_cmd._load_traces(str(store), None)
        assert len(traces) == 1
        assert traces[0].tags == []  # coerced to empty, not split into single characters
        assert "a" not in traces[0].tags

    def test_a_corrupt_lesson_file_is_skipped_with_a_warning_not_a_crash(self, store, capsys):
        """Same bug, other call site: _existing_source_traces read every
        existing lesson's frontmatter unguarded too."""
        main(["init", "--agent-type", "support", "--dest", str(store)])
        for i in range(3):
            _capture(
                store,
                f"Refund confusion {i}",
                "customer confused about refund timeline contradictory docs escalated",
                "point to canonical refund policy page",
            )
        lessons_dir = store / "memory" / "lessons"
        os.makedirs(lessons_dir, exist_ok=True)
        (lessons_dir / "lesson_corrupt.md").write_text(
            "---\nname: [unclosed\n---\n\nbody\n", encoding="utf-8"
        )
        capsys.readouterr()
        assert main(["distill", "--dest", str(store)]) == 0
        err = capsys.readouterr().err
        assert "skipping unreadable lesson" in err
        assert "lesson_corrupt.md" in err


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
        # A real, written lesson -- `lesson new` scaffolds every one of these
        # fields with a placeholder, and `lesson approve` now refuses to
        # activate a lesson still carrying them (an active lesson is injected
        # into agents verbatim). These tests are about the status transition
        # and slug resolution, so they approve the thing an operator would
        # actually be approving.
        fm["applies_when"] = "A refund retry returns HTTP 409 from the gateway"
        fm["do_not_apply_when"] = "The original charge was never authorized"
        body = (
            "## Rule\nReuse the original charge's idempotency key on the retry.\n\n"
            "## Why\nObserved across six refund incidents.\n\n"
            "## How to apply\nRead the key from the first charge, resend it.\n\n"
            "## Counter-examples\nDoes not apply to disputes.\n"
        )
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


class TestACandidateCarriesItsEvidence:
    """The throughput limit on this whole product is how expensive a lesson is
    to write, and a proposal used to make it as expensive as possible: one
    line per trace, context only, with a UUID on each -- so a 12-trace cluster
    printed the same paragraph twelve times and the SOLUTION TEXT, the one
    thing anyone needs in order to write the Rule, appeared nowhere.

    Coverage stays low because curating is expensive; retrieval returns
    nothing because coverage is low; the experiment stays underpowered
    because there is nothing to measure. This is the top of that chain.
    """

    @staticmethod
    def _cluster(n=6, context="A large CSV export returned a zero-byte file with no error.",
                 solutions=None):
        from commontrace import distill

        solutions = solutions or ["The worker was OOM-killed silently. Re-ran with date chunking."]
        return distill.Cluster(
            traces=[
                distill.TraceCandidate(
                    id=f"t{i}", path=f"/x/t{i}.md",
                    title="Export job silently produces an empty file",
                    context_text=context, solution_text=solutions[i % len(solutions)],
                    tags=["exports"], agent_type="support",
                )
                for i in range(n)
            ],
            shared_terms=("csv", "empty", "export"),
        )

    def _body(self, cluster) -> str:
        from commontrace.commands.distill_cmd import _candidate_body

        return "\n".join(_candidate_body(cluster))

    def test_what_worked_is_in_the_candidate(self):
        """Writing a lesson used to mean opening every source trace to find
        what had actually resolved it."""
        body = self._body(self._cluster())
        assert "**What worked**" in body
        assert "Re-ran with date chunking" in body

    def test_repeated_evidence_is_collapsed_with_counts(self):
        body = self._body(self._cluster(n=12))
        assert body.count("zero-byte file") == 1
        assert "(12 of 12)" in body

    def test_divergent_solutions_are_all_shown_and_flagged(self):
        """The signal that matters most: one symptom with several different
        resolutions is more than one problem, and writing it up as a single
        rule produces a lesson that fires on cases it cannot help."""
        body = self._body(self._cluster(n=9, solutions=[
            "Their IdP clock had drifted; synced NTP.",
            "The ACS URL pointed at our old domain; updated it.",
            "A SameSite policy blocked the session cookie; set it to None; Secure.",
        ]))
        assert "3 different resolutions" in body
        assert "consider splitting" in body
        for fragment in ("synced NTP", "ACS URL", "SameSite"):
            assert fragment in body

    def test_a_single_resolution_is_not_flagged(self):
        assert "different resolutions" not in self._body(self._cluster())

    def test_the_judgement_fields_stay_as_scaffolding(self):
        """Proposing better evidence is honest; proposing the conclusion is
        not. Filling `applies_when` or the Rule from a term-frequency count
        would push fabricated text past the guard that exists to stop exactly
        that."""
        from commontrace import templates

        body = self._body(self._cluster())
        assert body.count("TODO:") >= 3
        assert templates.unfilled_placeholders({}, body)

    def test_variants_beyond_the_cap_are_counted_not_dropped(self):
        body = self._body(self._cluster(
            n=8, solutions=[f"Distinct resolution number {i}." for i in range(8)]))
        assert "further variant(s)" in body


class TestTheProposedDescriptionIsReadable:
    """`description` is both what a curator reads in `lesson list` and a
    ranked retrieval field. It used to be the cluster's shared TERMS -- so a
    store with a dozen candidates was a dozen indistinguishable word lists,
    and the candidate matched queries on tokens like "anywhere" and "byte".
    """

    @staticmethod
    def _cluster(titles):
        from commontrace import distill

        return distill.Cluster(
            traces=[
                distill.TraceCandidate(
                    id=f"t{i}", path=f"/x/t{i}.md", title=title,
                    context_text="Customer reported the reset email never arrived.",
                    solution_text="Removed the address from the suppression list.",
                    tags=["email"], agent_type="support",
                )
                for i, title in enumerate(titles)
            ],
            shared_terms=("email", "reset"),
        )

    def test_it_is_a_sentence_not_a_word_list(self):
        from commontrace import distill

        cluster = self._cluster(["Password reset email never arrived"] * 4)
        description = distill.propose_description(cluster)
        assert description.startswith("Password reset email never arrived")
        assert "repeated pattern around" not in description

    def test_it_says_how_many_it_stands_for(self):
        from commontrace import distill

        assert "and 3 more like it" in distill.propose_description(
            self._cluster(["Password reset email never arrived"] * 4))

    def test_a_single_trace_needs_no_suffix(self):
        from commontrace import distill

        assert distill.propose_description(self._cluster(["Only one"])) == "Only one"

    def test_it_is_deterministic(self):
        """A proposal that changes text between two identical runs is one
        nobody can review."""
        from commontrace import distill

        cluster = self._cluster(["Alpha problem", "Beta problem", "Gamma problem"])
        assert len({distill.propose_description(cluster) for _ in range(10)}) == 1

    def test_it_picks_the_most_typical_trace_not_the_first(self):
        """The medoid, so an outlier that happens to sort first does not get
        to name the whole cluster."""
        from commontrace import distill

        cluster = self._cluster([
            "An unrelated outlier about billing invoices",
            "Password reset email never arrived",
            "Password reset email never arrived for a customer",
            "Password reset email never arrived again",
        ])
        assert "Password reset" in distill.representative(cluster).title

    def test_it_falls_back_when_there_is_no_title(self):
        from commontrace import distill

        description = distill.propose_description(self._cluster(["", ""]))
        assert "repeated pattern" in description
