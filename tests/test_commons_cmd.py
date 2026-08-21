"""Tests for `commontrace commons` — the client half of the cross-org commons.

The property that matters most here is that this client and hub/commons.py
sign the *same text the same way*. If they drift, nothing raises:
estimate_jaccard compares mismatched positions and returns a confident,
wrong similarity, and the resulting coverage number is quietly garbage.
That is the worst failure mode available for a number this product intends
to quote to customers, so it is pinned explicitly.
"""
from __future__ import annotations

import json

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


class TestContributeIsSafeByDefault:
    """Sharing is effectively publication -- withdrawal stops future
    matches but cannot retract what another org already retrieved. So the
    bulk path must never share without an explicit, informed instruction.
    """

    def _args(self, **over):
        base = dict(
            tags="", query="", limit=50, rationale="", confirm=False,
            hub_url="http://hub.invalid/mcp", hub_api_key="ct_live_test",
        )
        base.update(over)
        return type("A", (), base)()

    def test_refuses_an_unnarrowed_selection(self, capsys, monkeypatch):
        """No tags and no query would mean 'every trace you own'."""
        called = []
        monkeypatch.setattr(commons_cmd.hub_client, "_call_tool",
                            lambda *a, **k: called.append(a))
        assert commons_cmd.run_contribute(self._args()) == 1
        assert "refusing" in capsys.readouterr().err.lower()
        assert called == [], "must not have contacted the Hub at all"

    def test_preview_lists_candidates_but_shares_nothing(self, capsys, monkeypatch):
        async def fake_call(hub, key, tool, args):
            assert tool == "search_traces"
            return {"traces": [
                {"id": "aaaaaaaa-1111", "title": "Stripe webhook retries",
                 "tags": ["stripe"], "shared_with_commons": False},
                {"id": "bbbbbbbb-2222", "title": "CUDA grid limit",
                 "tags": ["cuda"], "shared_with_commons": False},
            ], "has_more": False}

        shared = []
        async def fake_share(*a, **k):
            shared.append(a)
            return {"id": "x"}

        monkeypatch.setattr(commons_cmd.hub_client, "_call_tool", fake_call)
        monkeypatch.setattr(commons_cmd.hub_client, "share_trace", fake_share)

        assert commons_cmd.run_contribute(self._args(tags="stripe,cuda")) == 0
        out = capsys.readouterr().out
        assert "Stripe webhook retries" in out
        assert "PREVIEW ONLY" in out
        assert shared == [], "preview must not share anything"

    def test_confirm_shares_only_the_unshared_ones(self, capsys, monkeypatch):
        async def fake_call(hub, key, tool, args):
            return {"traces": [
                {"id": "aaaaaaaa-1111", "title": "New", "tags": ["stripe"],
                 "shared_with_commons": False},
                {"id": "cccccccc-3333", "title": "Already in commons",
                 "tags": ["stripe"], "shared_with_commons": True},
            ], "has_more": False}

        shared = []
        async def fake_share(hub, key, tid, rationale=""):
            shared.append(tid)
            return {"id": tid}

        monkeypatch.setattr(commons_cmd.hub_client, "_call_tool", fake_call)
        monkeypatch.setattr(commons_cmd.hub_client, "share_trace", fake_share)

        assert commons_cmd.run_contribute(self._args(tags="stripe", confirm=True)) == 0
        assert shared == ["aaaaaaaa-1111"], "already-shared traces must be skipped"

    def test_nothing_to_share_is_reported_cleanly(self, capsys, monkeypatch):
        async def fake_call(hub, key, tool, args):
            return {"traces": [
                {"id": "cccccccc-3333", "title": "Already", "tags": ["stripe"],
                 "shared_with_commons": True},
            ], "has_more": False}
        monkeypatch.setattr(commons_cmd.hub_client, "_call_tool", fake_call)
        assert commons_cmd.run_contribute(self._args(tags="stripe", confirm=True)) == 0
        assert "nothing new to share" in capsys.readouterr().out.lower()


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
            "note": "No other org has contributed to the commons yet.",
        })
        assert "No other org has contributed" in rendered

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
