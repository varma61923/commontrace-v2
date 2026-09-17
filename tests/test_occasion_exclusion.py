"""Tests for holdout_io.injected_slugs_for_occasion and its two consumers:
`commontrace query --exclude-shown` and MCP `retrieve(exclude_shown=...)`.

The gap this closes: mem0's run_id scopes a query to one session; this
codebase's nearest equivalent (occasion_id) was recorded for holdout
assignment but never usable as a retrieval FILTER, so a long multi-turn
task had no way to avoid re-injecting the same guidance on every call.
"""
from __future__ import annotations

import os

import pytest

from commontrace import holdout_io, lesson_io, paths


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("COMMONTRACE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestInjectedSlugsForOccasion:
    def test_returns_empty_set_with_no_log_at_all(self, store):
        assert holdout_io.injected_slugs_for_occasion(str(store), "occ-1") == set()

    def test_returns_empty_string_occasion_as_empty_set(self, store):
        assert holdout_io.injected_slugs_for_occasion(str(store), "") == set()

    def test_returns_only_slugs_actually_injected_not_withheld(self, store):
        holdout_io.assign_and_log(
            str(store), ["a", "b"], occasion_id="occ-1", rate=1.0, salt="s",
        )
        # rate=1.0 -- everything eligible is withheld, so nothing was shown.
        assert holdout_io.injected_slugs_for_occasion(str(store), "occ-1") == set()

    def test_returns_injected_slugs_for_the_named_occasion(self, store):
        holdout_io.assign_and_log(
            str(store), ["a", "b"], occasion_id="occ-1", rate=0.0, salt="s",
        )
        assert holdout_io.injected_slugs_for_occasion(str(store), "occ-1") == {"a", "b"}

    def test_does_not_leak_across_occasions(self, store):
        holdout_io.assign_and_log(
            str(store), ["a"], occasion_id="occ-1", rate=0.0, salt="s",
        )
        holdout_io.assign_and_log(
            str(store), ["b"], occasion_id="occ-2", rate=0.0, salt="s",
        )
        assert holdout_io.injected_slugs_for_occasion(str(store), "occ-1") == {"a"}
        assert holdout_io.injected_slugs_for_occasion(str(store), "occ-2") == {"b"}

    def test_accumulates_across_multiple_calls_for_the_same_occasion(self, store):
        """The realistic multi-turn shape: the same occasion_id is used
        across several `commontrace query --experiment` calls as a task
        progresses."""
        holdout_io.assign_and_log(
            str(store), ["a"], occasion_id="occ-1", rate=0.0, salt="s",
        )
        holdout_io.assign_and_log(
            str(store), ["b"], occasion_id="occ-1", rate=0.0, salt="s",
        )
        assert holdout_io.injected_slugs_for_occasion(str(store), "occ-1") == {"a", "b"}


def _lesson(root: str, slug: str, description: str, **fm_extra) -> None:
    path = os.path.join(paths.lessons_dir(root), f"lesson_{slug}.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fm = {"name": slug, "description": description, "status": "active",
          "importance": 3, "tags": []}
    fm.update(fm_extra)
    lesson_io.write_lesson(path, fm, "body", root=root, actor="test", reason="fixture")


class TestExcludeShownThroughTheCliQueryCommand:
    def _args(self, root: str, task: str, **extra):
        import argparse

        base = dict(
            task=task, top_k=10, dest=root, agent_type=None, lexical=True,
            relevance_floor=None, include_importance_floor=None,
            experiment=False, occasion_id=None, holdout_rate=None,
            experiment_salt=None, exclude_shown=None,
        )
        base.update(extra)
        return argparse.Namespace(**base)

    def test_a_previously_shown_lesson_is_excluded_on_the_next_call(self, store, capsys):
        """`not_yet` did not exist during the first (logging) call, so it
        cannot have been marked injected for occ-1 -- only `shown` was.
        Both exist and match identically by the second call, isolating
        exclusion as the only thing that can explain `shown`'s absence."""
        from commontrace.commands import query_cmd

        _lesson(str(store), "shown", "refund policy for enterprise accounts")
        rc = query_cmd.run(self._args(
            str(store), "refund policy for enterprise accounts",
            experiment=True, occasion_id="occ-1", holdout_rate=0.0,
            experiment_salt="s",
        ))
        assert rc == 0
        capsys.readouterr()

        _lesson(str(store), "not_yet", "refund policy for enterprise accounts")
        rc = query_cmd.run(self._args(
            str(store), "refund policy for enterprise accounts",
            exclude_shown="occ-1",
        ))
        assert rc == 0
        out = capsys.readouterr().out
        slugs = {line.split()[0] for line in out.splitlines() if "rel=" in line}
        assert slugs == {"not_yet"}

    def test_a_core_lesson_is_never_excluded_even_if_the_log_names_it(self, store, capsys):
        """Core lessons never enter `eligible` for holdout assignment in
        the first place (`_run_lexical`'s own `eligible = [... if not
        c.core]`), so the natural flow can never produce a log entry
        naming one -- this seeds the log directly to prove the exemption
        itself is correct defense-in-depth, not merely untriggered."""
        from commontrace.commands import query_cmd

        _lesson(str(store), "always_on", "refund policy for enterprise accounts", core=True)
        _lesson(str(store), "matched", "refund policy for enterprise accounts")
        holdout_io.assign_and_log(
            str(store), ["always_on", "matched"], occasion_id="occ-1", rate=0.0, salt="s",
        )

        rc = query_cmd.run(self._args(
            str(store), "refund policy for enterprise accounts",
            exclude_shown="occ-1",
        ))
        assert rc == 0
        out = capsys.readouterr().out
        slugs = {line.split()[0] for line in out.splitlines() if "rel=" in line or "[core]" in line}
        assert slugs == {"always_on"}

    def test_a_non_experiment_prior_call_leaves_nothing_to_exclude(self, store, capsys):
        """No holdout log entry means no record, not "nothing was shown" --
        this is the honest limitation stated in the flag's own help text."""
        from commontrace.commands import query_cmd

        _lesson(str(store), "shown", "refund policy enterprise accounts")
        rc = query_cmd.run(self._args(
            str(store), "refund policy enterprise accounts",
        ))
        assert rc == 0
        capsys.readouterr()

        rc = query_cmd.run(self._args(
            str(store), "refund policy enterprise accounts",
            exclude_shown="occ-1",
        ))
        assert rc == 0
        out = capsys.readouterr().out
        assert "shown" in out

    def test_the_flag_is_a_no_op_when_not_given(self, store, capsys):
        from commontrace.commands import query_cmd

        _lesson(str(store), "shown", "refund policy enterprise accounts")
        query_cmd.run(self._args(
            str(store), "refund policy enterprise accounts",
            experiment=True, occasion_id="occ-1", holdout_rate=0.0,
            experiment_salt="s",
        ))
        capsys.readouterr()
        rc = query_cmd.run(self._args(str(store), "refund policy enterprise accounts"))
        assert rc == 0
        assert "shown" in capsys.readouterr().out
