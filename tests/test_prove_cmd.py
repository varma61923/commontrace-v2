"""Tests for `commontrace prove` — the client path to the Hub's causal
instrument.

The gap this closes is worth naming, because it was mine and it repeated a
mistake one layer up. `holdout_assign`, `record_occasion_outcome` and
`fleet_outcomes` were added to the Hub and wired to nothing a customer
could reach: no `hub_client` function, no CLI command. A fleet would have
had to hand-write MCP calls to run the experiment that STRATEGY.md §13.2
calls the cheapest falsifier available. Building an instrument and leaving
it where the users are not is the exact failure §19 corrected for the Hub,
committed again at the client boundary in the same session.

The rendering is what most of these test, because the ordering carries a
claim: the causal answer is printed FIRST when one exists. Burying it
under the observational table invites a reader to quote the weaker number
because they saw it first, and the weaker number is the one that dies to
"what else changed that quarter?".
"""
from __future__ import annotations

import argparse

from commontrace.commands import prove_cmd


def _args(**kw):
    base = {
        "hub_url": "https://hub.example.com",
        "hub_api_key": "ct_test",
        "agent_type": "",
        "json": False,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def _causal(effects, running=True, n_obs=120, n_occ=60):
    return {
        "experiment_running": running,
        "holdout_rate": 0.2,
        "n_observations": n_obs,
        "n_occasions": n_occ,
        "effects": effects,
        "note": "CAUSAL, unlike the before/after comparison alongside it.",
    }


def _effect(verdict, title="retry with jittered backoff", effect=0.29):
    return {
        "trace_id": "t-1", "title": title,
        "n_injected": 465, "n_withheld": 235,
        "rate_injected": 0.66, "rate_withheld": 0.37,
        "effect": effect, "ci_95": [0.217, 0.367], "p_value": 0.0001,
        "significant": True, "min_detectable_effect": 0.09,
        "verdict": verdict, "note": "Injecting this lesson causes better outcomes.",
    }


def _report(causal):
    return {
        "n_baseline_traces": 120, "n_current_traces": 240,
        "headline": "2 of 4 metric(s) improved measurably since the baseline window.",
        "metrics": [{
            "metric": "resolution_rate", "direction": "up",
            "baseline": {"rate": 0.55, "n": 120}, "current": {"rate": 0.78, "n": 240},
            "delta": 0.23, "ci_95": [0.14, 0.31], "p_value": 0.0,
            "significant": True, "min_detectable_effect": 0.09,
            "verdict": "improved", "note": "",
        }],
        "cost": [],
        "caveat": "OBSERVED CHANGE, NOT A CAUSAL EFFECT.",
        "causal": causal,
    }


class TestRenderOrdering:
    def test_the_causal_answer_is_printed_before_the_observational_one(self):
        """The ordering IS the claim. A reader who meets the before/after
        table first will quote it, and it is the number that dies to 'what
        else changed that quarter?'."""
        out = prove_cmd.render_outcomes(_report(_causal([_effect("HELPS")])))
        assert out.index("randomized holdout") < out.index("Observed change")

    def test_a_helping_lesson_shows_its_interval_and_p_value(self):
        out = prove_cmd.render_outcomes(_report(_causal([_effect("HELPS")])))
        assert "HELPS" in out
        assert "+29.0%" in out
        assert "95% CI [+21.7%, +36.7%]" in out

    def test_a_harmful_lesson_is_rendered_with_equal_prominence(self):
        """The verdict correlational scoring cannot produce, so it must not
        be softened or buried when it appears."""
        out = prove_cmd.render_outcomes(_report(_causal([_effect("HURTS", effect=-0.24)])))
        assert "HURTS" in out
        assert "-24.0%" in out

    def test_an_underpowered_result_shows_its_note_not_a_ci(self):
        e = _effect("UNDERPOWERED")
        e["note"] = "12 injected / 9 withheld; 30 needed in each arm. This is 'cannot answer yet', not 'no effect'."
        out = prove_cmd.render_outcomes(_report(_causal([e])))
        assert "cannot answer yet" in out
        assert "p=" not in out

    def test_no_experiment_says_nothing_here_is_causal(self):
        """The most important empty state: a reader must not take the
        observational table below for a causal result."""
        out = prove_cmd.render_outcomes(_report(_causal([], running=False, n_obs=0, n_occ=0)))
        assert "No experiment is running, so nothing below is causal" in out
        assert "start-experiment" in out

    def test_a_running_but_unmeasured_experiment_says_which_calls_are_missing(self):
        out = prove_cmd.render_outcomes(_report(_causal([], running=True, n_obs=0, n_occ=0)))
        assert "nothing has been measured yet" in out
        assert "prove assign" in out and "prove record" in out

    def test_the_observational_caveat_always_survives(self):
        out = prove_cmd.render_outcomes(_report(_causal([_effect("HELPS")])))
        assert "NOT A CAUSAL EFFECT" in out


class TestAssign:
    def test_withheld_traces_are_labelled_and_the_warning_is_shown(
        self, capsys, monkeypatch
    ):
        monkeypatch.setattr(
            prove_cmd.hub_client, "holdout_assign",
            lambda *a, **k: _async({
                "occasion_id": "ticket-1", "holdout_rate": 0.2,
                "inject": ["t-a"], "withhold": ["t-b"],
                "note": "Withheld traces must NOT be used on this occasion.",
            }),
        )
        assert prove_cmd.run_assign(_args(occasion_id="ticket-1", trace_ids=["t-a", "t-b"])) == 0
        out = capsys.readouterr().out
        assert "INJECT   t-a" in out
        assert "WITHHOLD t-b" in out
        assert "must NOT be used" in out

    def test_no_eligible_traces_is_explained_not_silent(self, capsys, monkeypatch):
        monkeypatch.setattr(
            prove_cmd.hub_client, "holdout_assign",
            lambda *a, **k: _async({
                "occasion_id": "ticket-1", "holdout_rate": 0.2,
                "inject": [], "withhold": [], "note": "",
            }),
        )
        assert prove_cmd.run_assign(_args(occasion_id="ticket-1", trace_ids=["nope"])) == 0
        assert "no eligible traces" in capsys.readouterr().out

    def test_a_hub_error_is_reported_not_raised(self, capsys, monkeypatch):
        def boom(*a, **k):
            raise prove_cmd.hub_client.HubConnectionError(
                "holdout_assign failed: experiment_not_running: no experiment"
            )

        monkeypatch.setattr(prove_cmd.hub_client, "holdout_assign", boom)
        assert prove_cmd.run_assign(_args(occasion_id="t", trace_ids=["x"])) == 1
        assert "experiment_not_running" in capsys.readouterr().err


class TestRecord:
    def test_a_resolved_occasion_reports_the_count(self, capsys, monkeypatch):
        monkeypatch.setattr(
            prove_cmd.hub_client, "record_occasion_outcome",
            lambda *a, **k: _async({"occasion_id": "ticket-1", "observations_resolved": 3}),
        )
        assert prove_cmd.run_record(
            _args(occasion_id="ticket-1", succeeded=True, failed=False)
        ) == 0
        assert "3 observation(s) resolved" in capsys.readouterr().out

    def test_zero_resolved_names_both_likely_causes(self, capsys, monkeypatch):
        """A bare '0 resolved' leaves a user with no idea whether they
        double-reported or never assigned."""
        monkeypatch.setattr(
            prove_cmd.hub_client, "record_occasion_outcome",
            lambda *a, **k: _async({"occasion_id": "ticket-1", "observations_resolved": 0}),
        )
        assert prove_cmd.run_record(
            _args(occasion_id="ticket-1", succeeded=True, failed=False)
        ) == 0
        out = capsys.readouterr().out
        assert "already reported" in out
        assert "never called" in out


class TestParser:
    def test_record_requires_an_explicit_outcome(self):
        """--succeeded and --failed are a required mutually exclusive
        group: defaulting to success would quietly bias every unreported
        task toward the treated arm looking good."""
        import pytest

        parser = argparse.ArgumentParser()
        prove_cmd.add_parser(parser.add_subparsers(dest="cmd"))
        with pytest.raises(SystemExit):
            parser.parse_args(["prove", "record", "ticket-1"])

    def test_record_accepts_either_outcome(self):
        parser = argparse.ArgumentParser()
        prove_cmd.add_parser(parser.add_subparsers(dest="cmd"))
        assert parser.parse_args(["prove", "record", "t", "--succeeded"]).succeeded is True
        assert parser.parse_args(["prove", "record", "t", "--failed"]).succeeded is False


def _async(value):
    """asyncio.run() needs a coroutine; the CLI wraps every hub_client call
    in one, so a monkeypatched stand-in has to return one too."""
    async def _inner():
        return value

    return _inner()
