"""Activating a lesson that restates one already active is refused.

Before this, nothing checked whether a lesson being approved already
existed in the corpus. An agent curating unattended re-derives the same
rule from a second trace cluster and has no reason to notice -- the corpus
grows two lessons saying one thing, and they compete for the same
retrieval slot forever with neither winning reliably (the `contradiction`/
`reliability.py` machinery catches lessons that DISAGREE; nothing caught
two that agree on the same words).

The check runs at APPROVAL, not at creation (`lesson new` / `propose_lessons`):
a freshly scaffolded lesson's body is template placeholder text
("## Rule\\n[1 actionable sentence]"), identical across every fresh lesson
and worthless to compare -- by approval time the content is real. Same gate
family as the existing scaffolding and content-safety checks
(`commontrace/commands/lesson_cmd.py:run_approve`,
`commontrace/mcp_server.py`'s `approve_lesson`): refuse by default, name
what was found, and (CLI only) an explicit `--force` for a human who has
looked and judged it a false positive. The MCP tool has no such override,
for the same reason its content-safety refusal has none: an agent
approving its own draft has no interactive human to confirm a deliberate
override.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys

import pytest

from commontrace import lesson_io, mcp_server, paths
from commontrace.commands import lesson_cmd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SAME_RULE = (
    "Never retry a payment without an idempotency key on the write path "
    "itself, not the entry point. Persist the provider's event id and "
    "check it before any side effect runs."
)


def _draft(root: str, slug: str, description: str, applies_when: str,
           do_not_apply_when: str = "counterexample",
           body: str = f"## Rule\n{SAME_RULE}\n") -> str:
    """Write a lesson at status=review with REAL content (not template
    scaffolding), directly through the journaling path -- the shape both
    `run_approve` and `approve_lesson` expect to activate."""
    path = os.path.join(paths.lessons_dir(root), f"lesson_{slug}.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fm = {
        "name": slug, "status": "review", "description": description,
        "applies_when": applies_when, "do_not_apply_when": do_not_apply_when,
        "importance": 3, "importance_rationale": "fixture", "tags": [],
        "agent_type": "support", "domain": "fixture", "uses": 0, "last_hit": "NEVER",
    }
    lesson_io.write_lesson(path, fm, body, root=root, actor="test", reason="fixture")
    return path


def _approve(root: str, slug: str, *, force: bool = False) -> int:
    args = argparse.Namespace(slug=slug, rationale="", force=force, dest=root)
    return lesson_cmd.run_approve(args)


class TestCliApproveRefusesARestatement:
    def test_a_restatement_of_an_active_lesson_is_refused(self, tmp_path, capsys):
        root = str(tmp_path)
        _draft(root, "first", "Payment webhook delivered more than once.",
               "A webhook is retried after a timeout or non-2xx response.")
        assert _approve(root, "first") == 0

        _draft(root, "second", "Duplicate charge from a retried payment webhook.",
               "A webhook is retried after a timeout or non-2xx response.")
        rc = _approve(root, "second")
        assert rc == 1
        err = capsys.readouterr().err
        assert "restates the active lesson" in err
        assert "'first'" in err

    def test_force_overrides_and_warns(self, tmp_path, capsys):
        root = str(tmp_path)
        _draft(root, "first", "Payment webhook delivered more than once.",
               "A webhook is retried after a timeout or non-2xx response.")
        assert _approve(root, "first") == 0
        _draft(root, "second", "Duplicate charge from a retried payment webhook.",
               "A webhook is retried after a timeout or non-2xx response.")

        rc = _approve(root, "second", force=True)
        assert rc == 0
        capsys.readouterr()  # drain the refusal-avoided path's own output
        from commontrace import frontmatter
        fm, _ = frontmatter.read(lesson_cmd._resolve_lesson_path(root, "second"))
        assert fm["status"] == "active"

    def test_distinct_lessons_are_never_refused(self, tmp_path, capsys):
        root = str(tmp_path)
        _draft(root, "payments", "Payment webhook delivered more than once.",
               "A webhook is retried after a timeout.",
               body=SAME_RULE)
        assert _approve(root, "payments") == 0

        _draft(root, "security", "Credentials rotated too infrequently.",
               "A service account key is older than the rotation policy allows.",
               body="Rotate service account credentials every ninety days.")
        rc = _approve(root, "security")
        assert rc == 0, capsys.readouterr()

    def test_a_review_candidate_is_not_compared_against_other_review_candidates(
        self, tmp_path
    ):
        """Only ACTIVE lessons compete for a retrieval slot. Two unrelated
        drafts sitting in review, one of which happens to restate the
        other, are not yet a problem -- approving the first is."""
        root = str(tmp_path)
        _draft(root, "first", "Payment webhook delivered more than once.",
               "A webhook is retried after a timeout.")
        _draft(root, "second", "Duplicate charge from a retried payment webhook.",
               "A webhook is retried after a timeout.")
        # Neither is active yet, so approving either must not be blocked by
        # the other one merely existing at status=review.
        assert _approve(root, "first") == 0

    def test_re_approving_the_same_lesson_does_not_match_itself(self, tmp_path):
        """A lesson approved twice (e.g. a retried operator command) must
        not be reported as a restatement of its own prior activation."""
        root = str(tmp_path)
        _draft(root, "first", "Payment webhook delivered more than once.",
               "A webhook is retried after a timeout.")
        assert _approve(root, "first") == 0
        # Force it back to review and re-approve -- still must not
        # self-match at similarity 1.0.
        from commontrace import frontmatter
        path = lesson_cmd._resolve_lesson_path(root, "first")
        fm, body = frontmatter.read(path)
        fm["status"] = "review"
        lesson_io.write_lesson(path, fm, body, root=root, actor="test", reason="reopen")
        assert _approve(root, "first") == 0


class TestMcpApproveLessonRefusesARestatement:
    """Same gate, over the tool surface an agent with no terminal uses."""

    @pytest.fixture
    def store(self, tmp_path):
        root = str(tmp_path / "fleet")
        result = subprocess.run(
            [sys.executable, "-m", "commontrace.cli", "init", "--dest", root,
             "--agent-type", "support"],
            capture_output=True, text=True, cwd=REPO_ROOT, check=False,
        )
        assert result.returncode == 0, result.stderr
        return root

    @pytest.fixture
    def server(self, store):
        # The SDK is an optional extra (`pip install commontrace[serve]`) --
        # the base client installs with PyYAML alone -- so this skips rather
        # than fails on an install that deliberately does not have it,
        # matching tests/test_mcp_server.py's own module-level guard and
        # tests/test_hybrid_retrieval.py's per-test one. In a fixture rather
        # than repeated in every test method: every test in this class needs
        # the server, so one guard here covers all of them.
        pytest.importorskip("mcp", reason="`commontrace serve` needs the MCP SDK: "
                                            "pip install 'commontrace[serve]'")
        return mcp_server.build_server(store)

    def _call(self, server, name: str, **arguments) -> dict:
        result = asyncio.run(server.call_tool(name, arguments))
        if getattr(result, "structured_content", None):
            sc = result.structured_content
            return sc.get("result", sc)
        return json.loads(result.content[0].text)

    def test_activation_is_refused_and_names_the_original(self, store, server):
        _draft(store, "first", "Payment webhook delivered more than once.",
               "A webhook is retried after a timeout or non-2xx response.")
        assert self._call(server, "approve_lesson", slug="first")["ok"]

        _draft(store, "second", "Duplicate charge from a retried payment webhook.",
               "A webhook is retried after a timeout or non-2xx response.")
        out = self._call(server, "approve_lesson", slug="second")
        assert not out["ok"]
        assert out.get("duplicate_of") == "first"
        assert "restates the active lesson" in out["error"]

        from commontrace import frontmatter
        fm, _ = frontmatter.read(lesson_cmd._resolve_lesson_path(store, "second"))
        assert fm["status"] == "review", "a refused activation must not change status"

    def test_distinct_lessons_are_never_refused(self, store, server):
        _draft(store, "payments", "Payment webhook delivered more than once.",
               "A webhook is retried after a timeout.", body=SAME_RULE)
        assert self._call(server, "approve_lesson", slug="payments")["ok"]

        _draft(store, "security", "Credentials rotated too infrequently.",
               "A service account key is older than the rotation policy allows.",
               body="Rotate service account credentials every ninety days.")
        out = self._call(server, "approve_lesson", slug="security")
        assert out["ok"], out

    def test_editing_the_draft_to_differ_clears_the_refusal(self, store, server):
        """The intended remedy: an agent that reads the refusal can
        genuinely differentiate the draft with `draft_lesson`, then
        succeed, without any override flag."""
        _draft(store, "first", "Payment webhook delivered more than once.",
               "A webhook is retried after a timeout or non-2xx response.")
        assert self._call(server, "approve_lesson", slug="first")["ok"]

        _draft(store, "second", "Duplicate charge from a retried payment webhook.",
               "A webhook is retried after a timeout or non-2xx response.")
        assert not self._call(server, "approve_lesson", slug="second")["ok"]

        self._call(
            server, "draft_lesson", slug="second",
            description="Retried webhooks should back off exponentially, distinct "
                         "from the idempotency-key concern.",
            applies_when="A webhook consumer is retrying on a fixed interval and "
                         "overwhelming the provider.",
            do_not_apply_when="The provider already enforces its own backoff.",
            rule="Use exponential backoff with jitter for webhook retry delivery.",
        )
        out = self._call(server, "approve_lesson", slug="second")
        assert out["ok"], out
