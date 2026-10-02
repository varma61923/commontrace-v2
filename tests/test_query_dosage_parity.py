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
        experiment_salt=None, exclude_shown=None,
    )
    base.update(extra)
    return argparse.Namespace(**base)


class TestBudgetThroughTheCliQueryCommand:
    def test_a_tight_budget_limits_what_the_command_prints(self, store, capsys):
        _lesson(store, "first", "Password reset email suppression failures.",
                "Check suppression list before resending.")
        _lesson(store, "second", "Password reset link expiry handling.",
                "Regenerate the link rather than resending the old one.")
        retrieval_io.configure(store, max_lessons=1, scorer="idf-v3")

        rc = query_cmd.run(_args(store, "password reset email"))
        assert rc == 0
        out = capsys.readouterr().out
        admitted = {line.split()[0] for line in out.splitlines() if "rel=" in line}
        assert admitted == {"first"} or admitted == {"second"}
        assert "not injected" in out
        assert "budget: 1/1" in out

    def test_a_core_lesson_is_admitted_even_when_it_does_not_match(self, store, capsys):
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
        _lesson(store, "on_call", "Escalate to on-call after two failed retries.",
                "Page on-call once a retry budget is exhausted.", core=True)
        _lesson(store, "matched", "Password reset email suppression failures.",
                "Check suppression list before resending.")
        retrieval_io.configure(store, max_lessons=1)

        rc = query_cmd.run(_args(store, "password reset email"))
        assert rc == 0
        out = capsys.readouterr().out
        assert "on_call" in out and "[core]" in out
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
        retrieval_io.configure(store, redundancy_threshold=0.3, scorer="idf-v3")

        rc = query_cmd.run(_args(store, "payment webhook delivered twice retried"))
        assert rc == 0
        out = capsys.readouterr().out
        admitted = {line.split()[0] for line in out.splitlines() if "rel=" in line}
        assert admitted == {"first"} or admitted == {"second"}
        assert "redundant with" in out

    def test_off_by_default_two_restatements_both_appear(self, store, capsys):
        same_rule = (
            "Never retry a payment without an idempotency key on the write "
            "path itself, not the entry point."
        )
        _lesson(store, "first", "Payment webhook delivered more than once.", same_rule)
        _lesson(store, "second", "Duplicate charge from a retried webhook.", same_rule)
        retrieval_io.configure(store, scorer="idf-v3")

        rc = query_cmd.run(_args(store, "payment webhook delivered twice retried"))
        assert rc == 0
        out = capsys.readouterr().out
        assert "first" in out and "second" in out


class TestScopedTemporalEligibility:
    def _retrieved(self, store, capsys, **args):
        rc = query_cmd.run(_args(store, "payment webhook retry policy", **args))
        output = capsys.readouterr()
        assert rc == 0, output
        return {line.split()[0] for line in output.out.splitlines() if "rel=" in line}

    def test_a_scope_includes_matching_and_global_lessons(self, store, capsys):
        _lesson(store, "global", "payment webhook retry policy", "global")
        _lesson(store, "payments", "payment webhook retry policy", "payments", scopes=["payments"])
        _lesson(store, "support", "payment webhook retry policy", "support", scopes=["support"])
        assert self._retrieved(store, capsys, scope="payments") == {"global", "payments"}

    def test_no_scope_keeps_the_backward_compatible_all_scopes_view(self, store, capsys):
        _lesson(store, "global", "payment webhook retry policy", "global")
        _lesson(store, "payments", "payment webhook retry policy", "payments", scopes=["payments"])
        _lesson(store, "support", "payment webhook retry policy", "support", scopes=["support"])
        assert self._retrieved(store, capsys) == {"global", "payments", "support"}

    def test_as_of_selects_the_valid_time_slice(self, store, capsys):
        _lesson(
            store, "old", "payment webhook retry policy", "old",
            valid_from="2023-01-01", valid_until="2025-01-01",
        )
        _lesson(
            store, "current", "payment webhook retry policy", "current",
            valid_from="2025-01-01", valid_until="2027-01-01",
        )
        _lesson(store, "future", "payment webhook retry policy", "future", valid_from="2027-01-01")
        assert self._retrieved(store, capsys, as_of="2026-06-01") == {"current"}
        assert self._retrieved(store, capsys, as_of="2024-06-01") == {"old"}

    def test_an_invalid_as_of_is_refused(self, store, capsys):
        _lesson(store, "global", "payment webhook retry policy", "global")
        assert query_cmd.run(_args(store, "payment webhook retry policy", as_of="not-a-date")) == 1
        assert "could not parse" in capsys.readouterr().err


class TestHoldoutOnlyCoversWhatWasActuallyAdministered:
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
        assert len(assigned_slugs) == 1
        assert assigned_slugs <= {"first", "second"}

    def test_a_core_lesson_is_never_assigned_an_arm(self, store, capsys):
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
    def _stub_semantic(self, monkeypatch, slugs, rc=0):
        monkeypatch.setattr(query_cmd, "has_attention_deps", lambda: True)
        monkeypatch.setattr(query_cmd, "_index_is_unusable", lambda root: "")
        monkeypatch.setattr(
            query_cmd, "_semantic_slugs",
            lambda args, root, hint, extra=0: (rc, list(slugs), ""),
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
    def _episode(self, store: str, name: str, verdict: str, retrieved: str, hit: str) -> None:
        import yaml

        eps_dir = os.path.join(store, "memory", "episodes")
        os.makedirs(eps_dir, exist_ok=True)
        fm = {"name": name, "verdict": verdict,
              "lessons_retrieved_by_alpha": [retrieved], "lessons_hit": [hit]}
        with open(os.path.join(eps_dir, f"{name}.md"), "w", encoding="utf-8") as fh:
            fh.write("---\n" + yaml.safe_dump(fm) + "---\n\nbody\n")

    def test_reliability_weight_reorders_two_topically_tied_lessons(self, store, capsys):
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
