"""Tests for `commontrace commons` — the client half of the CommonTrace
Knowledge Base.

The property that matters most here is that this client and hub/commons.py
sign the *same text the same way*. If they drift, nothing raises:
estimate_jaccard compares mismatched positions and returns a confident,
wrong similarity, and the resulting coverage number is quietly garbage.
That is the worst failure mode available for a number this product intends
to quote to customers, so it is pinned explicitly.
"""
from __future__ import annotations

import argparse
import json
import os

import pytest

from commontrace import overlap
from commontrace.commands import commons_cmd


def _write_trace(root, name, title, context, tags, repeated_error):
    tdir = root / "memory" / "traces"
    tdir.mkdir(parents=True, exist_ok=True)
    tag_line = "[" + ", ".join(tags) + "]"
    (tdir / f"{name}.md").write_text(
        "---\n"
        f"id: {name}\n"
        f"title: {title}\n"
        "agent_type: code\n"
        f"tags: {tag_line}\n"
        "profile: code-review\n"
        "outcome:\n"
        f"  repeated_error: {'true' if repeated_error else 'false'}\n"
        "---\n\n"
        f"## Context\n{context}\n\n## Solution\ns\n",
        encoding="utf-8",
    )


class TestSignaturesMatchTheHub:
    def test_client_signs_a_failure_the_same_way_the_hub_signs_a_trace(self, tmp_path):
        """The cross-boundary contract. hub/commons.py signs
        title + context + tags; this must produce a byte-identical
        signature for the same inputs, or every similarity score is wrong.
        """
        pytest.importorskip("hub.commons", reason="hub package not importable in this env")
        from hub import commons as hub_commons

        title, context, tags = "Stripe webhook retries", "duplicate delivery on 500", ["stripe"]
        _write_trace(tmp_path, "t1", title, context, tags, repeated_error=True)

        client_sigs = commons_cmd.build_signatures(str(tmp_path))
        assert len(client_sigs) == 1
        assert client_sigs[0]["signature"] == hub_commons.signature_for(title, context, tags)

    def test_client_uses_the_shared_num_perm(self):
        assert commons_cmd.COMMONS_NUM_PERM == overlap.DEFAULT_NUM_PERM


class TestBuildSignatures:
    def test_only_recurring_failures_are_signed(self, tmp_path):
        """A commons query asks "what do I keep paying for", so only traces
        that recorded a repeated error are submitted -- not every trace."""
        _write_trace(tmp_path, "recurring", "A", "ctx a", ["x"], repeated_error=True)
        _write_trace(tmp_path, "one-off", "B", "ctx b", ["y"], repeated_error=False)

        sigs = commons_cmd.build_signatures(str(tmp_path))
        assert [s["label"] for s in sigs] == ["recurring"]

    def test_signature_width_is_the_agreed_one(self, tmp_path):
        _write_trace(tmp_path, "t1", "A", "ctx", ["x"], repeated_error=True)
        sigs = commons_cmd.build_signatures(str(tmp_path))
        assert len(sigs[0]["signature"]) == commons_cmd.COMMONS_NUM_PERM

    def test_empty_store_yields_no_signatures(self, tmp_path):
        (tmp_path / "memory" / "traces").mkdir(parents=True)
        assert commons_cmd.build_signatures(str(tmp_path)) == []

    def test_malformed_tags_do_not_crash_signing(self, tmp_path):
        """Hand-edited frontmatter can put a scalar in `tags`. Signing must
        coerce rather than raise -- the same guard retrieval.py and
        overlap_cmd.py already apply."""
        tdir = tmp_path / "memory" / "traces"
        tdir.mkdir(parents=True)
        (tdir / "bad.md").write_text(
            "---\nid: bad\ntitle: T\nagent_type: code\ntags: 123\n"
            "outcome:\n  repeated_error: true\n---\n\n## Context\nc\n\n## Solution\ns\n",
            encoding="utf-8",
        )
        sigs = commons_cmd.build_signatures(str(tmp_path))
        assert len(sigs) == 1


class TestResolveHubWarnsOnCliApiKey:
    """A CLI argument is readable by any local user (`ps`, /proc/<pid>/
    cmdline) and can land in shell history / auditd's process-exec logs --
    none of which apply to COMMONTRACE_HUB_API_KEY. _resolve_hub warns when
    the key came from the command line, not the environment."""

    def _args(self, **over):
        base = dict(hub_url="http://hub.invalid/mcp", hub_api_key=None)
        base.update(over)
        return type("A", (), base)()

    def test_warns_when_key_passed_as_a_cli_flag(self, capsys):
        commons_cmd._resolve_hub(self._args(hub_api_key="ct_live_test"))
        err = capsys.readouterr().err
        assert "WARN" in err
        assert "--hub-api-key" in err

    def test_no_warning_when_key_comes_from_the_environment(self, capsys, monkeypatch):
        monkeypatch.setenv("COMMONTRACE_HUB_API_KEY", "ct_live_from_env")
        commons_cmd._resolve_hub(self._args(hub_api_key=None))
        err = capsys.readouterr().err
        assert "WARN" not in err


class TestSignCommandWritesSignaturesOnly:
    def test_written_file_contains_no_failure_text(self, tmp_path, capsys):
        """The privacy claim the whole self-serve flow rests on."""
        secret_title = "ZZQQ-CONFIDENTIAL-TITLE"
        secret_ctx = "WWXX-CONFIDENTIAL-CONTEXT"
        _write_trace(tmp_path, "t1", secret_title, secret_ctx, ["stripe"], repeated_error=True)
        out = tmp_path / "sigs.json"

        args = type("A", (), {"dest": str(tmp_path), "out": str(out)})()
        assert commons_cmd.run_sign(args) == 0

        raw = out.read_text(encoding="utf-8")
        assert secret_title not in raw
        assert secret_ctx not in raw
        assert "stripe" not in raw.lower()

        payload = json.loads(raw)
        assert payload["num_perm"] == commons_cmd.COMMONS_NUM_PERM
        assert len(payload["failures"]) == 1
        assert set(payload["failures"][0]) == {"label", "signature"}

    def test_reports_when_there_is_nothing_to_measure(self, tmp_path, capsys):
        (tmp_path / "memory" / "traces").mkdir(parents=True)
        out = tmp_path / "sigs.json"
        args = type("A", (), {"dest": str(tmp_path), "out": str(out)})()
        assert commons_cmd.run_sign(args) == 0
        assert "no recurring failures" in capsys.readouterr().err.lower()


class TestUsageShowsBonus:
    def _args(self, **over):
        base = dict(hub_url="http://hub.invalid/mcp", hub_api_key="ct_live_test")
        base.update(over)
        return type("A", (), base)()

    def _usage(self, bonus=0):
        return {
            "plan": "free", "period": "2026-01",
            "commons_queries": {
                "used": 3, "allowance": 20 + bonus, "remaining": 17 + bonus,
                "bonus_from_accepted_submissions": bonus,
            },
            "traces": {"used": 1, "limit": 1000},
        }

    def test_no_bonus_line_when_nothing_earned(self, capsys, monkeypatch):
        async def fake_usage(hub, key):
            return self._usage(bonus=0)

        monkeypatch.setattr(commons_cmd.hub_client, "account_usage", fake_usage)
        assert commons_cmd.run_usage(self._args()) == 0
        assert "earned via accepted" not in capsys.readouterr().out

    def test_bonus_line_shown_when_something_was_earned(self, capsys, monkeypatch):
        async def fake_usage(hub, key):
            return self._usage(bonus=25)

        monkeypatch.setattr(commons_cmd.hub_client, "account_usage", fake_usage)
        assert commons_cmd.run_usage(self._args()) == 0
        assert "25 earned via accepted `commons submit` proposals" in capsys.readouterr().out


class TestSubmit:
    """`commons submit` -- proposes a Knowledge Base entry for operator
    review. Nothing about this command publishes anything; it just calls
    the Hub's submit_kb_entry tool and reports the pending status back."""

    def _args(self, **over):
        base = dict(
            title="Stripe webhooks retry", context_text="duplicate delivery on 500",
            solution_text="use an idempotency key", tags="stripe,webhooks", agent_type="code",
            rationale="substrate, not our business logic",
            hub_url="http://hub.invalid/mcp", hub_api_key="ct_live_test",
        )
        base.update(over)
        return type("A", (), base)()

    def test_submits_with_parsed_tags_and_reports_pending_status(self, capsys, monkeypatch):
        captured = {}

        async def fake_submit(hub, key, **kwargs):
            captured.update(kwargs)
            return {"id": "sub-123", "status": "pending"}

        monkeypatch.setattr(commons_cmd.hub_client, "submit_kb_entry", fake_submit)

        assert commons_cmd.run_submit(self._args()) == 0
        assert captured["tags"] == ["stripe", "webhooks"]
        assert captured["title"] == "Stripe webhooks retry"
        assert captured["rationale"] == "substrate, not our business logic"

        out = capsys.readouterr().out
        assert "sub-123" in out
        assert "review" in out.lower()
        assert "Nothing is published yet" in out

    def test_a_hub_error_is_reported_not_raised(self, capsys, monkeypatch):
        async def fake_submit(hub, key, **kwargs):
            raise commons_cmd.hub_client.HubConnectionError("submit_kb_entry failed: invalid_request")

        monkeypatch.setattr(commons_cmd.hub_client, "submit_kb_entry", fake_submit)
        assert commons_cmd.run_submit(self._args()) == 1
        assert "invalid_request" in capsys.readouterr().err


class TestSubmissions:
    def _args(self, **over):
        base = dict(limit=None, json=False, hub_url="http://hub.invalid/mcp", hub_api_key="ct_live_test")
        base.update(over)
        return type("A", (), base)()

    def test_no_submissions_is_reported_cleanly(self, capsys, monkeypatch):
        async def fake_list(hub, key, limit=None):
            return {"submissions": []}

        monkeypatch.setattr(commons_cmd.hub_client, "list_my_kb_submissions", fake_list)
        assert commons_cmd.run_submissions(self._args()) == 0
        assert "no submissions yet" in capsys.readouterr().out.lower()

    def test_renders_status_for_each_submission(self, capsys, monkeypatch):
        async def fake_list(hub, key, limit=None):
            return {"submissions": [
                {"id": "s1", "status": "pending", "created_at": "2026-01-01T00:00:00+00:00",
                 "title": "A"},
                {"id": "s2", "status": "approved", "created_at": "2026-01-02T00:00:00+00:00",
                 "title": "B", "credit_awarded": 25},
                {"id": "s3", "status": "rejected", "created_at": "2026-01-03T00:00:00+00:00",
                 "title": "C", "rejection_reason": "too generic"},
            ]}

        monkeypatch.setattr(commons_cmd.hub_client, "list_my_kb_submissions", fake_list)
        assert commons_cmd.run_submissions(self._args()) == 0
        out = capsys.readouterr().out
        assert "status=pending" in out
        assert "+25 Knowledge Base queries credited" in out
        assert "too generic" in out

    def test_json_flag_emits_raw_json(self, capsys, monkeypatch):
        async def fake_list(hub, key, limit=None):
            return {"submissions": [{"id": "s1", "status": "pending"}]}

        monkeypatch.setattr(commons_cmd.hub_client, "list_my_kb_submissions", fake_list)
        assert commons_cmd.run_submissions(self._args(json=True)) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["submissions"][0]["id"] == "s1"


class TestRender:
    def test_headline_states_the_fraction(self):
        rendered = commons_cmd._render({
            "n_failures": 3, "n_covered": 2, "covered_fraction": 2 / 3,
            "n_commons_traces": 10, "threshold": 0.3,
            "by_agent_type": {"code": 2}, "matches": [], "note": "",
        })
        assert "**2 of 3**" in rendered
        assert "67%" in rendered

    def test_note_is_surfaced_not_buried(self):
        """A caveat about sample size is worthless if it isn't shown."""
        rendered = commons_cmd._render({
            "n_failures": 1, "n_covered": 0, "covered_fraction": 0.0,
            "n_commons_traces": 0, "threshold": 0.3,
            "by_agent_type": {}, "matches": [],
            "note": "The Knowledge Base has no entries yet, so this measures nothing.",
        })
        assert "Knowledge Base has no entries" in rendered

    def test_matches_include_the_solution(self):
        rendered = commons_cmd._render({
            "n_failures": 1, "n_covered": 1, "covered_fraction": 1.0,
            "n_commons_traces": 1, "threshold": 0.3, "by_agent_type": {"code": 1},
            "matches": [{
                "failure_label": "f1", "similarity": 0.51, "agent_type": "code",
                "tags": ["stripe"],
                "trace": {"title": "Stripe webhook", "solution_text": "Use an idempotency key"},
            }],
            "note": "",
        })
        assert "Stripe webhook" in rendered
        assert "Use an idempotency key" in rendered

    def test_disputed_matches_are_shown_and_marked_as_not_counted(self):
        """A coverage figure that fell because the field found an answer
        wrong is a different event from one that fell because the corpus
        shrank. A report that shows only the number hides the difference."""
        rendered = commons_cmd._render({
            "n_failures": 1, "n_covered": 0, "covered_fraction": 0.0,
            "n_commons_traces": 1, "threshold": 0.3, "by_agent_type": {},
            "matches": [],
            "disputed_matches": [{
                "failure_label": "f1", "similarity": 0.62, "agent_type": "code",
                "tags": ["react"],
                "trace": {
                    "title": "React 18 hydration workaround",
                    "solution_text": "Suppress the warning",
                    "vote_count": 6,
                    "standing": "disputed",
                },
            }],
            "note": "",
        })
        assert "disputed" in rendered.lower()
        assert "React 18 hydration workaround" in rendered
        assert "6 fleet(s)" in rendered
        assert "not counted above" in rendered.lower()

    def test_a_report_with_no_disputed_matches_says_nothing_about_them(self):
        """The section is evidence of a problem, so an absent problem must
        not print a heading suggesting there is one."""
        rendered = commons_cmd._render({
            "n_failures": 1, "n_covered": 1, "covered_fraction": 1.0,
            "n_commons_traces": 1, "threshold": 0.3, "by_agent_type": {"code": 1},
            "matches": [], "note": "",
        })
        assert "disputed" not in rendered.lower()


class TestRenderCandidates:
    """`commons ask` output. Unlike the coverage report, this one SHOWS
    disputed entries in the ordinary results (ranked last) -- so the
    warning has to travel with the entry, or a reader takes a contested
    answer for a corroborated one."""

    @staticmethod
    def _result(standing, vote_count=6, trust=0.17):
        return {
            "n_candidates": 1, "n_commons_traces": 4, "n_commons_traces_total": 4,
            "corpus_truncated": False, "note": "candidates, not coverage",
            "candidates": [{
                "rank": 1, "similarity": 0.42, "commons_hits": 3,
                "trace": {
                    "title": "React 18 hydration workaround",
                    "context_text": "SSR mismatch",
                    "solution_text": "Suppress the warning",
                    "tags": ["react"], "agent_type": "code",
                    "trust": trust, "vote_count": vote_count, "standing": standing,
                },
            }],
        }

    def test_a_disputed_candidate_carries_a_warning(self):
        rendered = commons_cmd._render_candidates(self._result("disputed"), "hydration error")
        assert "Disputed" in rendered
        assert "did not work" in rendered
        assert "Suppress the warning" in rendered

    def test_a_stale_candidate_says_it_is_overdue_not_wrong(self):
        rendered = commons_cmd._render_candidates(
            self._result("stale", vote_count=0, trust=0.5), "hydration error"
        )
        assert "review date" in rendered
        assert "Disputed" not in rendered

    def test_an_ordinary_candidate_gets_no_warning(self):
        rendered = commons_cmd._render_candidates(
            self._result("unproven", vote_count=0, trust=0.5), "hydration error"
        )
        assert "Disputed" not in rendered
        assert "review date" not in rendered

    def test_trust_is_shown_with_its_denominator(self):
        """0.00 from one downvote and 0.00 from twelve are the same number
        and completely different facts."""
        rendered = commons_cmd._render_candidates(
            self._result("disputed", vote_count=12, trust=0.0), "hydration error"
        )
        assert "trust 0.00 from 12 fleet(s)" in rendered

    def test_trust_is_hidden_entirely_when_nobody_has_voted(self):
        """0.5 with no votes is a column default, not a measurement, and
        printing it as one invites a reader to average it with real
        scores."""
        rendered = commons_cmd._render_candidates(
            self._result("unproven", vote_count=0, trust=0.5), "hydration error"
        )
        assert "trust" not in rendered


# --- Evaluating without adopting first ---------------------------------


class TestSignFromAnExistingExport:
    """The path that makes the thesis testable on day zero: a prospect with
    no memory/ directory, no captured traces, and an incident export."""

    def _export(self, tmp_path):
        p = os.path.join(str(tmp_path), "incidents.csv")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("Summary,Description\n"
                     "Pool exhausted,spike drained the connection pool\n"
                     "Duplicate charge,webhook redelivered after a timeout\n")
        return p

    def test_signs_without_any_commontrace_store(self, tmp_path, capsys):
        """No `commontrace init`, no captured traces. If this needed either,
        evaluating the product would require adopting it first."""
        out = os.path.join(str(tmp_path), "sig.json")
        rc = commons_cmd.run_sign(argparse.Namespace(
            out=out, from_file=self._export(tmp_path), dest=str(tmp_path),
        ))
        assert rc == 0
        with open(out, encoding="utf-8") as fh:
            payload = json.load(fh)
        assert len(payload["failures"]) == 2
        assert payload["num_perm"] == commons_cmd.COMMONS_NUM_PERM

    def test_says_what_it_read_and_what_travels(self, tmp_path, capsys):
        out = os.path.join(str(tmp_path), "sig.json")
        commons_cmd.run_sign(argparse.Namespace(
            out=out, from_file=self._export(tmp_path), dest=str(tmp_path),
        ))
        printed = capsys.readouterr().out
        assert "as csv" in printed
        assert "Failure text is NOT in this file" in printed
        # The labels DO leave, and for an import they are the prospect's own
        # incident titles. Saying so at the moment they decide to send it.
        assert "Labels" in printed and "ARE in this file" in printed

    def test_a_bad_file_fails_with_a_usable_message(self, tmp_path, capsys):
        bad = os.path.join(str(tmp_path), "bad.jsonl")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("{not json}\n")
        rc = commons_cmd.run_sign(argparse.Namespace(
            out=os.path.join(str(tmp_path), "sig.json"), from_file=bad, dest=str(tmp_path),
        ))
        assert rc == 1
        assert "line 1" in capsys.readouterr().err

    def test_report_refuses_both_input_flags(self, tmp_path, capsys):
        rc = commons_cmd.run_report(argparse.Namespace(
            signatures="a.json", from_file="b.csv", threshold=None, counts_only=False,
            json=False, hub_url="http://x/mcp", hub_api_key="k", dest=str(tmp_path),
        ))
        assert rc == 1
        assert "not both" in capsys.readouterr().err


# --- `commons report --candidates`: the lookup the coverage bar hides ----


def _report_args(**kw):
    base = dict(
        signatures=None, from_file=None, from_format=None, threshold=None,
        counts_only=False, candidates=False,
        candidate_limit=commons_cmd.DEFAULT_CANDIDATE_LOOKUPS,
        json=False, hub_url="http://hub.test/mcp", hub_api_key="ct_live_test", dest=None,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def _stub_hub(monkeypatch, *, report, searches=None, search_error=None):
    """Stand in for the two Hub calls, recording what was asked."""
    calls: list[list[int]] = []

    async def fake_overlap(hub_url, api_key, failures, **kw):
        return report

    async def fake_search(hub_url, api_key, signature, **kw):
        calls.append(signature)
        if search_error is not None:
            raise commons_cmd.hub_client.HubConnectionError(search_error)
        return {"candidates": (searches or {}).get(tuple(signature), [])}

    monkeypatch.setattr(commons_cmd.hub_client, "commons_overlap", fake_overlap)
    monkeypatch.setattr(commons_cmd.hub_client, "commons_search", fake_search)
    return calls


def _failures():
    return [
        {"label": "alpha", "signature": [1, 1, 1]},
        {"label": "beta", "signature": [2, 2, 2]},
        {"label": "gamma", "signature": [3, 3, 3]},
    ]


def _coverage(n_covered=0, matches=None, disputed=None):
    return {
        "n_failures": 3, "n_covered": n_covered,
        "covered_fraction": n_covered / 3, "n_commons_traces": 46, "threshold": 0.3,
        "matches": matches or [], "disputed_matches": disputed or [],
    }


class TestTheReportNoLongerReadsAsAnEmptyKnowledgeBase:
    """The defect this closes, recorded in commons/eval/RESULTS.md as the
    single highest-value thing to fix in the codebase.

    A prospect's export of nine failures -- seven of which the corpus
    provably contained -- reported "0 of 9. 0%." The number was correct:
    the coverage bar buys a 0% false-positive rate by discarding roughly
    nine of every ten real answers. But "0%" and "this product knows
    nothing about my problems" are indistinguishable to a reader, and only
    the second one predicts what happens in the room.
    """

    def test_an_uncovered_report_explains_the_bar_and_offers_the_lookup(
        self, monkeypatch, capsys
    ):
        monkeypatch.setattr(commons_cmd, "build_signatures", lambda root: _failures())
        _stub_hub(monkeypatch, report=_coverage())

        assert commons_cmd.run_report(_report_args()) == 0
        out = capsys.readouterr().out
        assert "0 of 3" in out
        assert "not the same as an empty Knowledge Base" in out
        assert "--candidates" in out
        # The cost is stated up front: each lookup is a metered consultation.
        assert "consultation" in out

    def test_a_fully_covered_report_makes_no_such_offer(self, monkeypatch, capsys):
        """Nothing was hidden, so there is nothing to explain away."""
        monkeypatch.setattr(commons_cmd, "build_signatures", lambda root: _failures())
        matches = [{"failure_label": f["label"], "similarity": 0.9, "trace": {"title": "t"}}
                   for f in _failures()]
        _stub_hub(monkeypatch, report=_coverage(n_covered=3, matches=matches))

        assert commons_cmd.run_report(_report_args()) == 0
        out = capsys.readouterr().out
        assert "empty Knowledge Base" not in out


class TestCandidatesAreLookedUpButNeverCounted:
    def test_it_looks_up_exactly_the_uncovered_failures(self, monkeypatch, capsys):
        monkeypatch.setattr(commons_cmd, "build_signatures", lambda root: _failures())
        matches = [{"failure_label": "alpha", "similarity": 0.9, "trace": {"title": "covered"}}]
        calls = _stub_hub(
            monkeypatch,
            report=_coverage(n_covered=1, matches=matches),
            searches={(2, 2, 2): [{"rank": 1, "similarity": 0.15,
                                   "trace": {"title": "beta answer", "solution_text": "do x"}}]},
        )

        assert commons_cmd.run_report(_report_args(candidates=True)) == 0
        # alpha cleared the bar, so looking it up again would spend a
        # consultation to repeat what the report already said.
        assert calls == [[2, 2, 2], [3, 3, 3]]
        out = capsys.readouterr().out
        assert "beta answer" in out
        assert "do x" in out

    def test_a_disputed_match_is_not_looked_up_again(self, monkeypatch):
        """`_render` already gives disputed matches their own section
        explaining why they are excluded from the figure. The Knowledge
        Base demonstrably has something about them, so they are found, not
        uncovered."""
        monkeypatch.setattr(commons_cmd, "build_signatures", lambda root: _failures())
        disputed = [{"failure_label": "beta", "similarity": 0.4, "trace": {"title": "d"}}]
        calls = _stub_hub(
            monkeypatch, report=_coverage(n_covered=0, disputed=disputed))

        commons_cmd.run_report(_report_args(candidates=True))
        assert [1, 1, 1] in calls and [3, 3, 3] in calls
        assert [2, 2, 2] not in calls

    def test_the_coverage_figure_is_untouched_by_the_lookups(self, monkeypatch, capsys):
        """The whole point. Ranked hits are candidates a human judges; the
        score distributions of true and absent matches overlap
        (commons/eval/RESULTS.md), so counting one would destroy the 0%
        false-positive property the quotable number rests on."""
        monkeypatch.setattr(commons_cmd, "build_signatures", lambda root: _failures())
        _stub_hub(
            monkeypatch, report=_coverage(),
            searches={(s, s, s): [{"rank": 1, "similarity": 0.5,
                                   "trace": {"title": f"answer {s}", "solution_text": "x"}}]
                      for s in (1, 2, 3)},
        )

        assert commons_cmd.run_report(_report_args(candidates=True)) == 0
        out = capsys.readouterr().out
        assert "0 of 3" in out and "**0%**" in out
        assert "NOT counted as coverage" in out
        assert "answer 1" in out

    def test_json_marks_the_lookups_as_not_coverage(self, monkeypatch, capsys):
        """A machine consumer must not be able to add these to the
        numerator by mistake."""
        monkeypatch.setattr(commons_cmd, "build_signatures", lambda root: _failures())
        _stub_hub(monkeypatch, report=_coverage(),
                  searches={(1, 1, 1): [{"rank": 1, "trace": {"title": "a"}}]})

        assert commons_cmd.run_report(_report_args(candidates=True, json=True)) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["n_covered"] == 0
        assert payload["covered_fraction"] == 0
        assert payload["candidate_lookups_are_not_coverage"] is True
        assert len(payload["candidate_lookups"]) == 3


class TestTheLookupsAreBoundedAndSurviveFailure:
    def test_the_budget_caps_lookups_and_says_how_many_were_skipped(
        self, monkeypatch, capsys
    ):
        """Each lookup is a metered consultation, so an unbounded report
        over a large import could spend a month's allowance in one
        command."""
        monkeypatch.setattr(commons_cmd, "build_signatures", lambda root: _failures())
        calls = _stub_hub(monkeypatch, report=_coverage())

        commons_cmd.run_report(_report_args(candidates=True, candidate_limit=1))
        assert len(calls) == 1
        out = capsys.readouterr().out
        assert "2 further uncovered failure(s) were not looked up" in out

    def test_a_zero_budget_spends_nothing(self, monkeypatch):
        monkeypatch.setattr(commons_cmd, "build_signatures", lambda root: _failures())
        calls = _stub_hub(monkeypatch, report=_coverage())
        commons_cmd.run_report(_report_args(candidates=True, candidate_limit=0))
        assert calls == []

    def test_a_failed_lookup_does_not_discard_the_report(self, monkeypatch, capsys):
        """A plan running out of consultations mid-report is the expected
        case, not an exceptional one -- and the coverage report was already
        paid for before the first lookup was attempted."""
        monkeypatch.setattr(commons_cmd, "build_signatures", lambda root: _failures())
        _stub_hub(monkeypatch, report=_coverage(), search_error="allowance exhausted")

        assert commons_cmd.run_report(_report_args(candidates=True)) == 0
        out = capsys.readouterr().out
        assert "0 of 3" in out
        assert "allowance exhausted" in out


# --- `commons report --corpus`: measured here, sent nowhere ---------------


def _local_args(tmp_path, corpus_records, export_records, **kw):
    cfile = tmp_path / "corpus.jsonl"
    cfile.write_text("\n".join(json.dumps(r) for r in corpus_records), encoding="utf-8")
    efile = tmp_path / "export.jsonl"
    efile.write_text("\n".join(json.dumps(r) for r in export_records), encoding="utf-8")
    base = dict(
        signatures=None, from_file=str(efile), from_format=None, threshold=None,
        counts_only=False, candidates=False,
        candidate_limit=commons_cmd.DEFAULT_CANDIDATE_LOOKUPS,
        corpus=str(cfile), semantic=False,
        json=False, hub_url=None, hub_api_key=None, dest=None,
    )
    base.update(kw)
    return argparse.Namespace(**base)


_CORPUS = [
    {"title": "Connection pool exhausted during a retry storm",
     "context_text": "callers queue waiting to acquire a connection",
     "tags": ["postgres"], "solution_text": "bound retries with jittered backoff"},
    {"title": "Payment webhook delivered more than once",
     "context_text": "the provider redelivers after a timeout",
     "tags": ["billing"], "solution_text": "persist the event id"},
]
_EXPORT = [
    {"title": "Connection pool exhausted during a retry storm",
     "context_text": "callers queue waiting to acquire a connection", "tags": ["postgres"]},
    {"title": "the office coffee machine leaks", "context_text": "water on the floor",
     "tags": ["facilities"]},
]


class TestALocalReportTouchesNothing:
    """The privacy property, asserted rather than described.

    The corpus is operator-curated public content and the failure text is
    already on this machine, so both halves of the comparison are local.
    That makes `--corpus` disclose strictly less than the shipped path,
    which transmits a MinHash signature: here the operator does not learn
    that a fleet asked, let alone what about. A regression that quietly
    reintroduced a Hub call would destroy exactly that, and silently.
    """

    def test_no_hub_call_is_made(self, tmp_path, monkeypatch, capsys):
        def explode(*a, **kw):
            raise AssertionError("a local report must not contact a Hub")

        monkeypatch.setattr(commons_cmd.hub_client, "commons_overlap", explode)
        monkeypatch.setattr(commons_cmd.hub_client, "commons_search", explode)

        assert commons_cmd.run_report(_local_args(tmp_path, _CORPUS, _EXPORT)) == 0
        assert "computed locally" in capsys.readouterr().out.lower()

    def test_it_needs_no_hub_url_or_api_key(self, tmp_path, capsys):
        """A prospect evaluating the corpus has no account yet."""
        assert commons_cmd.run_report(
            _local_args(tmp_path, _CORPUS, _EXPORT, hub_url=None, hub_api_key=None)) == 0
        out = capsys.readouterr().out
        assert "of 2" in out

    def test_the_lexical_matcher_reproduces_the_hubs_own_number(self, tmp_path, capsys):
        """`--corpus` alone runs the same overlap code the Hub runs, so an
        offline run is a check on the Hub rather than a different product."""
        commons_cmd.run_report(_local_args(tmp_path, _CORPUS, _EXPORT))
        out = capsys.readouterr().out
        # The identically-worded failure clears any sane bar; the coffee
        # machine clears none.
        assert "**1 of 2**" in out
        assert "Connection pool exhausted" in out


class TestTheThresholdsCostIsVisible:
    def test_a_near_miss_is_shown_rather_than_hidden(self, tmp_path, capsys):
        """A matcher that hides its own runner-up is how a threshold's cost
        becomes invisible -- the exact defect RESULTS.md records against the
        shipped coverage figure."""
        commons_cmd.run_report(_local_args(tmp_path, _CORPUS, _EXPORT))
        out = capsys.readouterr().out
        assert "below the bar, shown anyway" in out
        assert "office coffee machine" in out

    def test_near_misses_never_move_the_figure(self, tmp_path, capsys):
        commons_cmd.run_report(_local_args(tmp_path, _CORPUS, _EXPORT, json=True))
        payload = json.loads(capsys.readouterr().out)
        assert payload["n_covered"] == 1
        uncovered = [e for e in payload["entries"] if not e["matches"]]
        assert all(e["best_unmatched"] is not None for e in uncovered)


class TestSemanticIsOptOutAndFailsLoudly:
    def test_semantic_without_a_corpus_is_refused(self, tmp_path, capsys):
        """The Hub serves signatures, not embeddings, so there is nothing
        for --semantic to compare against remotely."""
        args = _local_args(tmp_path, _CORPUS, _EXPORT, corpus=None, semantic=True)
        assert commons_cmd.run_report(args) == 1
        assert "--semantic needs --corpus" in capsys.readouterr().err

    def test_a_missing_model_stack_is_reported_not_raised(
        self, tmp_path, monkeypatch, capsys
    ):
        """Never degrade silently to the other matcher: a coverage number
        computed by a different matcher than the caller asked for is the
        quiet substitution this codebase refuses elsewhere."""
        def unavailable(*a, **kw):
            raise commons_cmd.semantic.SemanticUnavailable("install the extra")

        monkeypatch.setattr(commons_cmd.semantic, "best_matches", unavailable)
        args = _local_args(tmp_path, _CORPUS, _EXPORT, semantic=True)
        assert commons_cmd.run_report(args) == 1
        err = capsys.readouterr().err
        assert "install the extra" in err

    def test_semantic_uses_the_measured_operating_point(self, tmp_path, monkeypatch, capsys):
        seen = {}

        def fake(queries, corpus, *, threshold, top_k=5):
            seen["threshold"] = threshold
            return [{"best": (0, 0.9), "matches": [(0, 0.9)]} for _ in queries]

        monkeypatch.setattr(commons_cmd.semantic, "best_matches", fake)
        commons_cmd.run_report(_local_args(tmp_path, _CORPUS, _EXPORT, semantic=True))
        # 0.65, not the higher-recall 0.60: that one leaks false positives on
        # the dev probe set (commons/eval/semantic.py).
        assert seen["threshold"] == commons_cmd.semantic.DEFAULT_SEMANTIC_THRESHOLD
        assert "semantic (cosine >= 0.65" in capsys.readouterr().out

    def test_an_explicit_threshold_still_wins(self, tmp_path, monkeypatch):
        seen = {}

        def fake(queries, corpus, *, threshold, top_k=5):
            seen["threshold"] = threshold
            return [{"best": None, "matches": []} for _ in queries]

        monkeypatch.setattr(commons_cmd.semantic, "best_matches", fake)
        commons_cmd.run_report(
            _local_args(tmp_path, _CORPUS, _EXPORT, semantic=True, threshold=0.5))
        assert seen["threshold"] == 0.5


class TestTheCorpusFileIsValidated:
    def test_a_malformed_corpus_is_refused_cleanly(self, tmp_path, capsys):
        bad = tmp_path / "bad.jsonl"
        bad.write_text("{not json}\n", encoding="utf-8")
        exp = tmp_path / "e.jsonl"
        exp.write_text(json.dumps(_EXPORT[0]) + "\n", encoding="utf-8")
        args = _local_args(tmp_path, _CORPUS, _EXPORT, corpus=str(bad), from_file=str(exp))
        assert commons_cmd.run_report(args) == 1
        assert "not valid JSON" in capsys.readouterr().err

    def test_a_record_without_a_title_is_refused(self, tmp_path, capsys):
        bad = tmp_path / "bad.jsonl"
        bad.write_text(json.dumps({"context_text": "x"}) + "\n", encoding="utf-8")
        args = _local_args(tmp_path, _CORPUS, _EXPORT, corpus=str(bad))
        assert commons_cmd.run_report(args) == 1
        assert "needs a title" in capsys.readouterr().err

    def test_corpus_without_from_is_refused(self, tmp_path, capsys):
        args = _local_args(tmp_path, _CORPUS, _EXPORT, from_file=None)
        assert commons_cmd.run_report(args) == 1
        assert "--corpus needs --from" in capsys.readouterr().err


# --- `commons fetch`: the one call that asks about nothing ---------------


class TestFetchingTheCorpus:
    """The call that removes the disclosure price of using the corpus.

    Every other Knowledge Base call describes a failure -- as a signature,
    but the Hub still learns that this fleet is asking and roughly about
    what. This one asks for public curated content and names nothing, so
    afterwards `report --corpus` needs no network at all.
    """

    def _args(self, tmp_path, **kw):
        base = dict(
            out=str(tmp_path / "corpus.jsonl"), limit=None,
            hub_url="http://hub.test/mcp", hub_api_key="ct_live_test",
        )
        base.update(kw)
        return argparse.Namespace(**base)

    def _stub(self, monkeypatch, result):
        seen = {}

        async def fake_export(hub_url, api_key, limit=None):
            seen["limit"] = limit
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(commons_cmd.hub_client, "commons_export", fake_export)
        return seen

    def test_it_writes_the_corpus_as_jsonl(self, tmp_path, monkeypatch, capsys):
        self._stub(monkeypatch, {"entries": [
            {"title": "a", "context_text": "c", "solution_text": "s", "tags": ["t"]},
            {"title": "b", "context_text": "c", "solution_text": "s", "tags": []},
        ], "n_entries": 2})
        args = self._args(tmp_path)
        assert commons_cmd.run_fetch(args) == 0

        written = [json.loads(x) for x in
                   open(args.out, encoding="utf-8").read().splitlines() if x.strip()]
        assert [r["title"] for r in written] == ["a", "b"]
        # The file it writes must be the file --corpus reads.
        assert commons_cmd._load_corpus(args.out) == written

    def test_it_tells_you_the_next_command(self, tmp_path, monkeypatch, capsys):
        """The point of holding the corpus is the offline run; a fetch that
        does not say so leaves the privacy gain undiscovered."""
        self._stub(monkeypatch, {"entries": [{"title": "a", "solution_text": "s"}],
                                 "n_entries": 1})
        commons_cmd.run_fetch(self._args(tmp_path))
        out = capsys.readouterr().out
        assert "--corpus" in out
        assert "nothing needs to leave this machine" in out

    def test_truncation_is_reported(self, tmp_path, monkeypatch, capsys):
        """Silently returning a partial corpus would make every later local
        coverage number quietly wrong."""
        self._stub(monkeypatch, {"entries": [{"title": "a", "solution_text": "s"}],
                                 "n_entries": 1, "truncated": True})
        commons_cmd.run_fetch(self._args(tmp_path))
        assert "truncated" in capsys.readouterr().err

    def test_a_disabled_export_is_reported_not_raised(self, tmp_path, monkeypatch, capsys):
        """A deployment may decline to publish its corpus in bulk. That is a
        configuration answer, not a crash."""
        self._stub(monkeypatch, commons_cmd.hub_client.HubConnectionError(
            "commons_export failed: entitlement_exceeded: ... "
            "ask the operator to set HUB_COMMONS_EXPORT_ENABLED=true."))
        assert commons_cmd.run_fetch(self._args(tmp_path)) == 1
        assert "HUB_COMMONS_EXPORT_ENABLED" in capsys.readouterr().err

    def test_an_empty_corpus_is_not_written_as_a_valid_file(
        self, tmp_path, monkeypatch, capsys
    ):
        """Writing an empty corpus would make the next `report --corpus`
        say 0% with total confidence, for the wrong reason."""
        self._stub(monkeypatch, {"entries": [], "n_entries": 0})
        args = self._args(tmp_path)
        assert commons_cmd.run_fetch(args) == 1
        assert not os.path.exists(args.out)

    def test_the_limit_is_passed_through(self, tmp_path, monkeypatch):
        seen = self._stub(monkeypatch, {"entries": [{"title": "a", "solution_text": "s"}],
                                        "n_entries": 1})
        commons_cmd.run_fetch(self._args(tmp_path, limit=10))
        assert seen["limit"] == 10
