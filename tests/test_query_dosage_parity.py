"""`commontrace query` and the MCP surface must serve the same treatment.

Before this, `commontrace query` applied ONLY `--top-k`: a store's
`core: true` lessons and its character/count budget (commontrace/dosage.py)
were enforced by `commontrace/mcp_server.py`'s `retrieve()` and nowhere
else. A fleet split across a human driving `commontrace query` and agents
retrieving over MCP was running two different treatments under one
experiment -- and, worse, the CLI logged a holdout assignment for every
ranked lesson up to `--top-k` regardless of whether the budget would ever
have let it reach the agent, which is exactly the "occasion counted as
treated where no memory was injected" failure `commontrace/mcp_server.py`'s
own `_apply_dosage` docstring warns pulls a measured effect toward zero.

These tests pin, on the CLI path, the same invariants
`tests/test_dosage_and_receipts.py` and `tests/test_mcp_server.py` already
pin on the primitive and the MCP surface respectively.
"""
from __future__ import annotations

import argparse
import os

import pytest

from commontrace import frontmatter, holdout_io, lesson_io, paths, retrieval_io
from commontrace.commands import query_cmd


def _lesson(root: str, slug: str, description: str, body: str, **fm_extra) -> None:
    path = os.path.join(paths.lessons_dir(root), f"lesson_{slug}.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fm = {"name": slug, "description": description, "status": "active",
          "importance": 3, "tags": []}
    fm.update(fm_extra)
    lesson_io.write_lesson(path, fm, body, root=root, actor="test", reason="fixture")
    assert frontmatter.read(path)


@pytest.fixture
def store(tmp_path):
    return str(tmp_path / "fleet")


def _args(root: str, task: str, **extra) -> argparse.Namespace:
    base = dict(
        task=task, top_k=10, dest=root, agent_type=None, lexical=True,
        relevance_floor=None, include_importance_floor=None,
        experiment=False, occasion_id=None, holdout_rate=None,
        experiment_salt=None,
    )
    base.update(extra)
    return argparse.Namespace(**base)


class TestBudgetThroughTheCliQueryCommand:
    def test_a_tight_budget_limits_what_the_command_prints(self, store, capsys):
        _lesson(store, "first", "Password reset email suppression failures.",
                "Check suppression list before resending.")
        _lesson(store, "second", "Password reset link expiry handling.",
                "Regenerate the link rather than resending the old one.")
        retrieval_io.configure(store, max_lessons=1)

        rc = query_cmd.run(_args(store, "password reset email"))
        assert rc == 0
        out = capsys.readouterr().out
        # Exactly one of the two matches is ADMITTED -- printed with its own
        # rel=... line -- while the other is only ever named in the "not
        # injected" accounting, never silently. Substring containment alone
        # would pass even if the dropped slug never made it into either
        # line, since "not injected: second (...)" also contains "second".
        admitted = {line.split()[0] for line in out.splitlines() if "rel=" in line}
        assert admitted == {"first"} or admitted == {"second"}
        assert "not injected" in out
        assert "budget: 1/1" in out

    def test_a_core_lesson_is_admitted_even_when_it_does_not_match(self, store, capsys):
        """The fleet's unconditional rule must reach the CLI surface exactly
        as it already reaches MCP's `retrieve()` -- present whether or not
        it matches today's vocabulary."""
        _lesson(store, "on_call", "Escalate to on-call after two failed retries.",
                "Page on-call once a retry budget is exhausted.", core=True)
        _lesson(store, "matched", "Password reset email suppression failures.",
                "Check suppression list before resending.")

        rc = query_cmd.run(_args(store, "password reset email"))
        assert rc == 0
        out = capsys.readouterr().out
        assert "on_call" in out and "[core]" in out
        assert "matched" in out

    def test_core_still_competes_for_the_count_budget(self, store, capsys):
        """A fleet that marks lessons core has not thereby earned unlimited
        budget -- commontrace/dosage.py's own stated invariant, now checked
        on the CLI path too."""
        _lesson(store, "on_call", "Escalate to on-call after two failed retries.",
                "Page on-call once a retry budget is exhausted.", core=True)
        _lesson(store, "matched", "Password reset email suppression failures.",
                "Check suppression list before resending.")
        retrieval_io.configure(store, max_lessons=1)

        rc = query_cmd.run(_args(store, "password reset email"))
        assert rc == 0
        out = capsys.readouterr().out
        assert "on_call" in out and "[core]" in out
        # The one count-budget slot went to core; the matched lesson was
        # never admitted at all -- named only in the "not injected" line.
        admitted = {line.split()[0] for line in out.splitlines() if "rel=" in line}
        assert admitted == set()
        assert "not injected: matched" in out

    def test_redundant_restatement_is_dropped_when_the_store_opts_in(self, store, capsys):
        same_rule = (
            "Never retry a payment without an idempotency key on the write "
            "path itself, not the entry point."
        )
        _lesson(store, "first", "Payment webhook delivered more than once.", same_rule)
        _lesson(store, "second", "Duplicate charge from a retried webhook.", same_rule)
        retrieval_io.configure(store, redundancy_threshold=0.3)

        rc = query_cmd.run(_args(store, "payment webhook delivered twice retried"))
        assert rc == 0
        out = capsys.readouterr().out
        admitted = {line.split()[0] for line in out.splitlines() if "rel=" in line}
        assert admitted == {"first"} or admitted == {"second"}
        assert "redundant with" in out

    def test_off_by_default_two_restatements_both_appear(self, store, capsys):
        """Suppressing an admitted lesson is a treatment change, never an
        upgrade side effect -- same posture as fusion and the budget."""
        same_rule = (
            "Never retry a payment without an idempotency key on the write "
            "path itself, not the entry point."
        )
        _lesson(store, "first", "Payment webhook delivered more than once.", same_rule)
        _lesson(store, "second", "Duplicate charge from a retried webhook.", same_rule)

        rc = query_cmd.run(_args(store, "payment webhook delivered twice retried"))
        assert rc == 0
        out = capsys.readouterr().out
        assert "first" in out and "second" in out


class TestHoldoutOnlyCoversWhatWasActuallyAdministered:
    """The bug this whole file exists for: an occasion must not be logged as
    treated with a lesson the budget never let through."""

    def test_a_budget_dropped_lesson_is_never_assigned_an_arm(self, store, capsys):
        _lesson(store, "first", "Password reset email suppression failures.",
                "Check suppression list before resending.")
        _lesson(store, "second", "Password reset link expiry handling.",
                "Regenerate the link rather than resending the old one.")
        retrieval_io.configure(store, max_lessons=1)

        rc = query_cmd.run(_args(
            store, "password reset email",
            experiment=True, occasion_id="occ-1", holdout_rate=0.0,
            experiment_salt="s",
        ))
        assert rc == 0, capsys.readouterr()

        records, unreadable = holdout_io.read_log(store)
        assert unreadable == 0
        assigned_slugs = {r.lesson for r in records}
        # Exactly one of the two was ever a candidate for randomization --
        # the other never reached the agent, so it must never appear in the
        # log as either arm.
        assert len(assigned_slugs) == 1
        assert assigned_slugs <= {"first", "second"}

    def test_a_core_lesson_is_never_assigned_an_arm(self, store, capsys):
        """Core lessons are unconditional and present in both arms by
        definition, so they cannot confound the comparison -- excluded from
        randomization on the CLI path exactly as on MCP's."""
        _lesson(store, "on_call", "Escalate to on-call after two failed retries.",
                "Page on-call once a retry budget is exhausted.", core=True)
        _lesson(store, "matched", "Password reset email suppression failures.",
                "Check suppression list before resending.")

        rc = query_cmd.run(_args(
            store, "password reset email",
            experiment=True, occasion_id="occ-2", holdout_rate=0.0,
            experiment_salt="s",
        ))
        assert rc == 0, capsys.readouterr()

        records, _ = holdout_io.read_log(store)
        assert {r.lesson for r in records} == {"matched"}

    def test_zero_budget_logs_no_experiment_requires_no_occasion_error(self, store, capsys):
        """When the budget admits nothing, there is nothing to randomize --
        this must be reported plainly, not crash or silently log an empty
        assignment."""
        _lesson(store, "first", "Password reset email suppression failures.",
                "Check suppression list before resending.")
        retrieval_io.configure(store, max_lessons=0)

        rc = query_cmd.run(_args(
            store, "password reset email",
            experiment=True, occasion_id="occ-3", holdout_rate=0.0,
            experiment_salt="s",
        ))
        assert rc == 0
        out = capsys.readouterr().out
        assert "admitted none" in out
        records, _ = holdout_io.read_log(store)
        assert records == []


class TestHybridBudgetParity:
    """`_run_hybrid` mirrors `_run_lexical`'s dosage wiring line for line --
    pinned separately because it is a distinct code path with its own
    printing (arms, rrf score) and its own eligibility label."""

    def _stub_semantic(self, monkeypatch, slugs, rc=0):
        monkeypatch.setattr(query_cmd, "has_attention_deps", lambda: True)
        monkeypatch.setattr(query_cmd, "_index_is_unusable", lambda root: "")
        monkeypatch.setattr(
            query_cmd, "_semantic_slugs",
            lambda args, root, hint: (rc, list(slugs), ""),
        )

    def test_a_tight_budget_limits_the_fused_output(self, store, monkeypatch, capsys):
        _lesson(store, "first", "Password reset email suppression failures.",
                "Check suppression list before resending.")
        _lesson(store, "second", "Password reset link expiry handling.",
                "Regenerate the link rather than resending the old one.")
        retrieval_io.configure(store, fusion="rrf", max_lessons=1)
        self._stub_semantic(monkeypatch, ["second"])

        rc = query_cmd.run(_args(store, "password reset email", lexical=False))
        assert rc == 0
        out = capsys.readouterr().out
        admitted = {line.split()[0] for line in out.splitlines() if "rrf=" in line}
        assert len(admitted) == 1
        assert "not injected" in out

    def test_a_core_lesson_reaches_the_fused_output_unmatched(self, store, monkeypatch, capsys):
        _lesson(store, "on_call", "Escalate to on-call after two failed retries.",
                "Page on-call once a retry budget is exhausted.", core=True)
        _lesson(store, "matched", "Password reset email suppression failures.",
                "Check suppression list before resending.")
        retrieval_io.configure(store, fusion="rrf")
        self._stub_semantic(monkeypatch, [])

        rc = query_cmd.run(_args(store, "password reset email", lexical=False))
        assert rc == 0
        out = capsys.readouterr().out
        assert "on_call" in out and "[core]" in out

    def test_holdout_on_the_fused_path_only_covers_what_was_admitted(
        self, store, monkeypatch, capsys
    ):
        _lesson(store, "first", "Password reset email suppression failures.",
                "Check suppression list before resending.")
        _lesson(store, "second", "Password reset link expiry handling.",
                "Regenerate the link rather than resending the old one.")
        retrieval_io.configure(store, fusion="rrf", max_lessons=1)
        self._stub_semantic(monkeypatch, ["second"])

        rc = query_cmd.run(_args(
            store, "password reset email", lexical=False,
            experiment=True, occasion_id="occ-fused-1", holdout_rate=0.0,
            experiment_salt="s",
        ))
        assert rc == 0, capsys.readouterr()
        records, unreadable = holdout_io.read_log(store)
        assert unreadable == 0
        assert len({r.lesson for r in records}) == 1


class TestReliabilityAndRecencyWeightingThroughTheCliQueryCommand:
    """End-to-end: `commontrace retrieval --reliability-weight`/
    `--recency-weight`, configured on disk, must actually reorder what
    `commontrace query` prints -- not just what the unit-level
    tests/test_retrieval.py pins on `rank_lessons` directly."""

    def _episode(self, store: str, name: str, verdict: str, retrieved: str, hit: str) -> None:
        import yaml

        eps_dir = os.path.join(store, "memory", "episodes")
        os.makedirs(eps_dir, exist_ok=True)
        fm = {"name": name, "verdict": verdict,
              "lessons_retrieved_by_alpha": [retrieved], "lessons_hit": [hit]}
        with open(os.path.join(eps_dir, f"{name}.md"), "w", encoding="utf-8") as fh:
            fh.write("---\n" + yaml.safe_dump(fm) + "---\n\nbody\n")

    def test_reliability_weight_reorders_two_topically_tied_lessons(self, store, capsys):
        # Identical description text -- these tie exactly on topical
        # relevance, so any ordering difference is attributable to the
        # reliability adjustment alone.
        _lesson(store, "good", "refund policy for enterprise accounts",
                "Follow the standard refund SOP.")
        _lesson(store, "bad", "refund policy for enterprise accounts",
                "An alternate, unreviewed refund approach.")
        for i in range(12):
            self._episode(store, f"good_ok{i}", "CONFORM", "good", "good")
        for i in range(12):
            self._episode(store, f"bad_fail{i}", "ABANDON", "bad", "bad")
        retrieval_io.configure(store, reliability_weight=0.3)

        rc = query_cmd.run(_args(store, "refund policy for enterprise accounts", top_k=2))
        assert rc == 0
        out = capsys.readouterr().out
        first_slug_line = next(line for line in out.splitlines() if "rel=" in line)
        assert first_slug_line.split()[0] == "good"

    def test_recency_weight_reorders_two_topically_tied_lessons(self, store, capsys):
        _lesson(store, "fresh", "refund policy for enterprise accounts",
                "Follow the standard refund SOP.", last_hit="2026-06-01")
        _lesson(store, "stale", "refund policy for enterprise accounts",
                "An older refund approach.", last_hit="NEVER")
        retrieval_io.configure(store, recency_weight=0.3)

        rc = query_cmd.run(_args(store, "refund policy for enterprise accounts", top_k=2))
        assert rc == 0
        out = capsys.readouterr().out
        first_slug_line = next(line for line in out.splitlines() if "rel=" in line)
        assert first_slug_line.split()[0] == "fresh"

    def test_off_by_default_tied_lessons_keep_their_insertion_tie_break(self, store, capsys):
        """With neither weight configured, reliability/recency evidence on
        disk must have zero effect -- confirms the feature is genuinely
        opt-in end-to-end, not just at the unit level."""
        _lesson(store, "good", "refund policy for enterprise accounts",
                "Follow the standard refund SOP.")
        _lesson(store, "bad", "refund policy for enterprise accounts",
                "An alternate, unreviewed refund approach.")
        for i in range(12):
            self._episode(store, f"bad_fail{i}", "ABANDON", "bad", "bad")

        rc = query_cmd.run(_args(store, "refund policy for enterprise accounts", top_k=2))
        assert rc == 0
        out = capsys.readouterr().out
        slugs = {line.split()[0] for line in out.splitlines() if "rel=" in line}
        assert slugs == {"good", "bad"}
