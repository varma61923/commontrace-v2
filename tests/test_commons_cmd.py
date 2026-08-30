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
