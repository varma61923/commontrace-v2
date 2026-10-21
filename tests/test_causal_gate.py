"""Causal gate: contextual effects, source reputation, Thompson schedules, drift and off-policy checks."""
from __future__ import annotations

import datetime
import json
import os
import random
import types

import pytest

from unimem import allocation, allocation_schedule, causal_gate, experiment, holdout_io, integrity
from unimem.causal_gate import Row
from unimem.cli import main
from unimem.commands import experiment_cmd

T0 = datetime.datetime(2026, 3, 1, 9, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture(autouse=True)
def _no_fsync(monkeypatch):
    monkeypatch.setattr(os, "fsync", lambda fd: None)


class _Clock:
    def __init__(self):
        self.now = T0

    def tick(self, seconds: float) -> None:
        self.now += datetime.timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()

    class Frozen(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return c.now

    frozen = types.SimpleNamespace(datetime=Frozen, timezone=datetime.timezone, timedelta=datetime.timedelta)
    monkeypatch.setattr(holdout_io, "datetime", frozen)
    monkeypatch.setattr(allocation, "datetime", frozen)
    return c


def _at(i: int) -> str:
    return (T0 + datetime.timedelta(seconds=i)).isoformat()


def simulate(rng, n, lessons, *, rate_for, effect_for, base_for=lambda ctx: 0.5,
             contexts=("code", "support"), start=0):
    """Occasions with every lesson independently randomized; the outcome adds each shown lesson's effect."""
    rows = []
    for i in range(start, start + n):
        ctx = contexts[i % len(contexts)]
        p = base_for(ctx)
        decisions = []
        for lesson in lessons:
            rate = rate_for(lesson, ctx)
            injected = rng.random() >= rate
            decisions.append((lesson, injected, rate))
            if injected:
                p += effect_for(lesson, ctx)
        succeeded = rng.random() < min(0.98, max(0.02, p))
        rows += [Row(lesson, f"o{i}", injected, succeeded, rate, ctx, _at(i)) for lesson, injected, rate in decisions]
    return rows


# -- estimation -----------------------------------------------------------------------------------


def test_the_contrast_is_unbiased_when_the_holdout_rate_depends_on_the_context():
    # Easy coding occasions are mostly shown the memory, hard support ones mostly not:
    # a raw difference in means credits the memory with the context's easiness.
    rng = random.Random(1)
    rows = simulate(rng, 20000, ["m"], rate_for=lambda _l, ctx: 0.1 if ctx == "code" else 0.7,
                    effect_for=lambda _l, _c: 0.1, base_for=lambda ctx: 0.8 if ctx == "code" else 0.2)
    shown = [r.succeeded for r in rows if r.injected]
    withheld = [r.succeeded for r in rows if not r.injected]
    naive = sum(shown) / len(shown) - sum(withheld) / len(withheld)
    estimate = causal_gate.contrast(rows)
    assert naive > 0.3
    assert estimate.effect == pytest.approx(0.1, abs=0.02)
    assert estimate.n_injected + estimate.n_withheld == len(rows)


def test_a_contrast_needs_both_arms():
    assert causal_gate.contrast([Row("m", "o1", True, True, 0.5)]) is None
    assert causal_gate.contrast([]) is None


def test_the_gate_learns_a_memory_that_helps_one_context_and_hurts_another():
    rng = random.Random(2)
    rows = simulate(rng, 4000, ["test-runner"], rate_for=lambda _l, _c: 0.5,
                    effect_for=lambda _l, ctx: 0.25 if ctx == "code" else -0.25)
    model = causal_gate.fit(rows)
    overall = model.lessons["test-runner"]
    code, support = model.contexts[("test-runner", "code")], model.contexts[("test-runner", "support")]
    assert abs(overall.mean) < 0.05  # the average hides both effects
    assert code.mean == pytest.approx(0.25, abs=0.05) and code.p_helps > 0.99
    assert support.mean == pytest.approx(-0.25, abs=0.05) and support.p_helps < 0.01
    rates, context_rates = causal_gate.rates(model, floor=0.05)
    assert context_rates["test-runner"] == {"code": 0.05, "support": 0.95}
    assert 0.05 < rates["test-runner"] < 0.95


def test_contexts_without_real_differences_stay_with_their_lesson():
    rng = random.Random(3)
    rows = simulate(rng, 3000, ["steady"], rate_for=lambda _l, _c: 0.5, effect_for=lambda _l, _c: 0.15,
                    contexts=("a", "b", "c"))
    model = causal_gate.fit(rows)
    _rates, context_rates = causal_gate.rates(model)
    assert model.tau_context == pytest.approx(causal_gate.CONTEXT_TAU_FLOOR)
    assert context_rates == {}


def test_a_lesson_is_not_shrunk_toward_its_own_data():
    rng = random.Random(4)
    rows = simulate(rng, 4000, ["solo"], rate_for=lambda _l, _c: 0.5, effect_for=lambda _l, _c: 0.2,
                    contexts=("x",))
    post = causal_gate.fit(rows).lessons["solo"]
    assert post.mean == pytest.approx(post.raw_effect, abs=0.01)


def test_source_reputation_sets_the_prior_for_a_memory_with_little_evidence():
    rng = random.Random(5)
    proven = {"bad-1": -0.3, "bad-2": -0.25, "bad-3": -0.3, "good-1": 0.3, "good-2": 0.25, "good-3": 0.3}
    rows = simulate(rng, 2000, list(proven), rate_for=lambda _l, _c: 0.5,
                    effect_for=lambda lesson, _c: proven[lesson] / 3, contexts=("x",))
    # Two brand-new memories with identical, uninformative evidence: 2 of 3 succeed in each arm.
    for lesson in ("new-bad", "new-good"):
        rows += [Row(lesson, f"{lesson}-{i}", i < 3, i % 3 != 0, 0.5, "x", _at(10_000 + i)) for i in range(6)]
    sources = {lesson: ("publisher:shady" if "bad" in lesson else "publisher:trusted") for lesson in proven}
    sources |= {"new-bad": "publisher:shady", "new-good": "publisher:trusted"}

    model = causal_gate.fit(rows, sources)
    assert model.sources["publisher:shady"].mean < 0 < model.sources["publisher:trusted"].mean
    assert model.lessons["new-bad"].p_helps < 0.5 < model.lessons["new-good"].p_helps
    rates, _ = causal_gate.rates(model)
    assert rates["new-bad"] > rates["new-good"]

    blind = causal_gate.fit(rows)  # without provenance, the two new memories are indistinguishable
    assert blind.lessons["new-bad"].mean == pytest.approx(blind.lessons["new-good"].mean)


def test_withhold_rates_are_clipped_and_the_floor_is_validated():
    assert causal_gate.withhold_rate(1.0) == 0.05
    assert causal_gate.withhold_rate(0.0) == 0.95
    assert causal_gate.withhold_rate(0.7, floor=0.1) == 0.3
    for bad in (0.0, 0.5, -1.0):
        with pytest.raises(ValueError):
            causal_gate.withhold_rate(0.5, floor=bad)


def test_lesson_source_reads_publisher_then_provenance_then_traces():
    assert causal_gate.lesson_source({"publisher": "Acme"}) == "publisher:Acme"
    assert causal_gate.lesson_source({"marketplace": {"publisher": "Acme"}}) == "publisher:Acme"
    assert causal_gate.lesson_source(
        {"provenance": {"source_kind": "explicit_host_history_scan", "source_agent": "cursor"}}
    ) == "kind:explicit_host_history_scan/cursor"
    assert causal_gate.lesson_source({"source_traces": ["t1"]}) == "distilled"
    assert causal_gate.lesson_source({}) == "local"


# -- drift ----------------------------------------------------------------------------------------


def test_drift_finds_a_memory_that_stopped_helping_and_leaves_stable_ones_alone():
    rng = random.Random(6)
    rows = simulate(rng, 1600, ["flips", "steady"], rate_for=lambda _l, _c: 0.5,
                    effect_for=lambda lesson, _c: 0.15 if lesson == "steady" else 0.3, contexts=("x",))
    rows += simulate(rng, 1600, ["flips", "steady"], rate_for=lambda _l, _c: 0.5,
                     effect_for=lambda lesson, _c: 0.15 if lesson == "steady" else -0.3, contexts=("x",),
                     start=1600)
    rows += [Row("rare", f"r{i}", i % 2 == 0, True, 0.5, "x", _at(9000 + i)) for i in range(6)]
    found = {d.lesson: d for d in causal_gate.drift(rows)}
    assert found["flips"].verdict == "DRIFTED"
    assert found["flips"].before > 0.15 and found["flips"].after < 0
    assert found["steady"].verdict == "STABLE"
    assert found["rare"].verdict == "INSUFFICIENT"


def test_a_drifted_memory_is_gated_on_its_evidence_since_the_change():
    rng = random.Random(12)
    rows = simulate(rng, 1600, ["flips"], rate_for=lambda _l, _c: 0.5, effect_for=lambda _l, _c: 0.3,
                    contexts=("x",))
    rows += simulate(rng, 800, ["flips"], rate_for=lambda _l, _c: 0.5, effect_for=lambda _l, _c: -0.3,
                     contexts=("x",), start=1600)
    stale = causal_gate.fit(rows).lessons["flips"]
    assert stale.mean > 0  # the old record still outweighs the new harm
    kept, resets = causal_gate.forget_before_drift(rows, causal_gate.drift(rows))
    assert set(resets) == {"flips"} and all(r.at >= resets["flips"] for r in kept)
    current = causal_gate.fit(kept).lessons["flips"]
    assert current.mean < -0.15 and causal_gate.withhold_rate(current.p_helps) == 0.95


def test_weak_drift_is_reported_but_never_forgets_evidence():
    rows = [Row("m", f"o{i}", i % 2 == 0, True, 0.5, "x", _at(i)) for i in range(10)]
    weak = causal_gate.Drift("m", "DRIFTED", _at(5), 0.2, 0.0, -0.2, 0.03, 0.03, 5, 5)
    kept, resets = causal_gate.forget_before_drift(rows, [weak])
    assert resets == {} and kept == rows
    strong = causal_gate.Drift("m", "DRIFTED", _at(5), 0.2, 0.0, -0.2, 0.001, 0.001, 5, 5)
    kept, resets = causal_gate.forget_before_drift(rows, [strong])
    assert resets == {"m": _at(5)} and len(kept) == 5


def test_stable_memories_are_rarely_reset():
    rng = random.Random(13)
    resets = 0
    for run in range(150):
        rows = simulate(rng, 600, [f"m{run}"], rate_for=lambda _l, _c: 0.5,
                        effect_for=lambda _l, ctx: 0.25 if ctx == "code" else -0.25)
        resets += bool(causal_gate.forget_before_drift(rows, causal_gate.drift(rows))[1])
    assert resets / 150 <= 0.04  # nominal 1%, with room for sampling noise


def test_benjamini_hochberg_matches_the_textbook_step_up():
    q = causal_gate._benjamini_hochberg([0.01, 0.04, 0.03, 0.5])
    assert q == pytest.approx([0.04, 0.0533333, 0.0533333, 0.5], abs=1e-6)


# -- off-policy evaluation -------------------------------------------------------------------------


def test_the_logging_policy_scores_exactly_what_was_delivered():
    rng = random.Random(7)
    rows = simulate(rng, 500, ["a", "b"], rate_for=lambda lesson, _c: 0.3 if lesson == "a" else 0.6,
                    effect_for=lambda _l, _c: 0.1)
    result = causal_gate.policy_value(rows, lambda lesson, _c: 0.3 if lesson == "a" else 0.6)
    assert result.lift == pytest.approx(0.0, abs=1e-12)
    assert result.effective_sample_size == pytest.approx(500)


def test_the_off_policy_check_credits_a_gate_that_injects_where_memories_help():
    rng = random.Random(8)
    rows = simulate(rng, 6000, ["test-runner"], rate_for=lambda _l, _c: 0.5,
                    effect_for=lambda _l, ctx: 0.25 if ctx == "code" else -0.25)
    result = causal_gate.evaluate(rows)
    # Delivered: half the occasions in each context see the memory, so the effects cancel.
    # The gate shows it on 95% of code occasions and 5% of support ones: +0.1125 expected.
    assert result.verdict == "IMPROVES"
    assert result.lift == pytest.approx(0.1125, abs=0.04)
    assert result.ci_low > 0 and result.fit_occasions == result.eval_occasions == 3000


def test_a_policy_far_from_the_logged_one_is_reported_as_unscoreable():
    rng = random.Random(9)
    lessons = [f"m{i}" for i in range(12)]
    rows = simulate(rng, 300, lessons, rate_for=lambda _l, _c: 0.5, effect_for=lambda _l, _c: 0.0)
    result = causal_gate.policy_value(rows, lambda _l, _c: 0.02)
    assert result.verdict == "INSUFFICIENT_OVERLAP" and result.effective_sample_size < 30


def test_off_policy_needs_data():
    assert causal_gate.evaluate([]).verdict == "INSUFFICIENT_DATA"
    assert causal_gate.policy_value([], lambda _l, _c: 0.5).verdict == "INSUFFICIENT_DATA"


# -- schedules and the assignment path -------------------------------------------------------------


def _schedule(**extra):
    return allocation_schedule.Schedule(version=0, salt="s", effective_from=T0.isoformat(), default_rate=0.5,
                                        rates={"a": 0.2}, policy={}, basis={}, previous="",
                                        created_at=T0.isoformat(), **extra)


def test_schedules_without_context_rates_hash_exactly_as_before():
    legacy_body = {"version": 0, "salt": "s", "effective_from": T0.isoformat(), "default_rate": 0.5,
                   "rates": {"a": 0.2}, "policy": {}, "basis": {}, "previous": "",
                   "created_at": T0.isoformat(), "enabled": True}
    assert _schedule().body() == legacy_body
    assert "context_rates" in _schedule(context_rates={"a": {"code": 0.05}}).body()


def test_a_schedule_sets_rates_per_context_and_the_audit_checks_them():
    schedule = _schedule(context_rates={"a": {"code": 0.05}})
    assert schedule.rate_for("a", "code") == 0.05
    assert schedule.rate_for("a", "support") == schedule.rate_for("a") == 0.2
    assert schedule.rate_for("b", "code") == 0.5
    later = T0 + datetime.timedelta(minutes=1)
    good = [integrity.Assignment("a", f"o{i}", True, rate=0.05, salt="s", at=later, schedule="x", context="code")
            for i in range(3)]
    good.append(integrity.Assignment("a", "o9", True, rate=0.2, salt="s", at=later, schedule="x", context="support"))
    assert allocation_schedule.unscheduled(good, [schedule]) == []
    wrong = integrity.Assignment("a", "o8", True, rate=0.05, salt="s", at=later, schedule="x", context="support")
    assert allocation_schedule.unscheduled([*good, wrong], [schedule]) == [wrong]
    assert integrity.check_assignment_drift([*good, wrong], [schedule]).severity == integrity.SEVERITY_INVALIDATES


@pytest.mark.parametrize("raw, label", [(None, ""), ("", ""), ("  Code   Review ", "code review"),
                                        ("Support", "support"), ("line\u2028break", "line break")])
def test_context_labels_are_normalized(raw, label):
    assert holdout_io.context_label(raw) == label


@pytest.mark.parametrize("raw", ["x" * 65, "a\x00b", "esc\x1b[0m", 7])
def test_context_labels_refuse_what_is_not_a_short_label(raw):
    with pytest.raises(ValueError):
        holdout_io.context_label(raw)


def test_thompson_eras_gate_per_context_and_the_whole_run_audits(tmp_path, clock):
    root = str(tmp_path)
    holdout_io.configure(root, rate=0.5, salt="pinned")
    with pytest.raises(ValueError, match="mode"):
        allocation.Policy(mode="greedy")
    first = allocation.enable(root, allocation.Policy(era_occasions=200, mode="thompson"))
    clock.tick(10)
    rng = random.Random(10)
    effect = {"code": 0.25, "support": -0.25}

    def occasions(start, n):
        for i in range(start, start + n):
            ctx = "code" if i % 2 == 0 else "support"
            occasion = f"T-{i}"
            withheld = holdout_io.assign_and_log(root, ["test-runner"], occasion_id=occasion, rate=0.5,
                                                 salt="pinned", revisions={"test-runner": "r1"}, context=ctx)
            p = 0.5 + (effect[ctx] if "test-runner" not in withheld else 0.0)
            holdout_io.record_outcome(root, occasion, rng.random() < p)
            clock.tick(30)

    occasions(0, 1200)
    second = allocation.plan(root)
    assert second.previous == first.digest and second.policy["mode"] == "thompson"
    assert second.context_rates["test-runner"] == {"code": 0.05, "support": 0.95}
    assert second.basis["mode"] == "thompson" and second.basis["resolved"] == 1200
    clock.tick(10)
    occasions(1200, 400)

    records, _ = holdout_io.read_log(root)
    later = [r for r in records if r.at >= second.effective_at()]
    assert later and all(r.rate == (0.05 if r.context == "code" else 0.95) for r in later)
    assert all(r.schedule == second.digest[:16] for r in later)
    assert allocation.verify(root) == []

    rows, _rate, _ = experiment_cmd._load(root)
    assert {r.context for r in rows} == {"code", "support"}
    report = integrity.audit(rows, schedules=allocation.history(root))
    drift_check = next(f for f in report.findings if f.check == "assignment_drift")
    assert drift_check.severity == integrity.SEVERITY_OK
    [effect_row] = experiment.analyze(experiment_cmd._observations(rows), sequential=True)
    assert effect_row.ci_low <= 0.0 <= effect_row.ci_high  # the average effect is still estimated honestly

    assert main(["allocate", "show", "--dest", root]) == 0
    out = causal_gate.report(root)
    assert {c["context"] for c in out["model"]["contexts"]} == {"code", "support"}
    assert out["off_policy"]["verdict"] in {"IMPROVES", "INCONCLUSIVE"}


def test_the_gate_command_reports_in_text_and_json(tmp_path, clock, capsys):
    root = str(tmp_path)
    assert main(["allocate", "gate", "--dest", root]) == 0
    assert "no resolved assignments yet" in capsys.readouterr().out
    holdout_io.configure(root, rate=0.5, salt="pinned")
    rng = random.Random(11)
    for i in range(400):
        ctx = "code" if i % 2 == 0 else "support"
        withheld = holdout_io.assign_and_log(root, ["m"], occasion_id=f"o{i}", rate=0.5, salt="pinned",
                                             revisions={"m": "r1"}, context=ctx)
        holdout_io.record_outcome(root, f"o{i}", rng.random() < 0.5 + (0.3 if "m" not in withheld else 0.0))
        clock.tick(5)
    assert main(["allocate", "gate", "--dest", root]) == 0
    text = capsys.readouterr().out
    assert "causal gate over 400 resolved assignments" in text and "in 'code'" in text
    assert main(["allocate", "gate", "--json", "--dest", root]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["model"]["lessons"][0]["lesson"] == "m" and doc["model"]["lessons"][0]["p_helps"] > 0.99
    assert main(["allocate", "enable", "--mode", "thompson", "--dest", root]) == 0
    assert "unlisted memories: 50% withheld" in capsys.readouterr().out


def test_a_plain_assignment_without_context_logs_no_context(tmp_path, clock):
    root = str(tmp_path)
    holdout_io.configure(root, rate=0.5, salt="pinned")
    holdout_io.assign_and_log(root, ["m"], occasion_id="o1", rate=0.5, salt="pinned", revisions={"m": None})
    with open(holdout_io.holdout_log_path(root), encoding="utf-8") as fh:
        assert "context" not in json.loads(fh.readline())
    [record] = holdout_io.read_log(root)[0]
    assert record.context is None


# -- surfaces --------------------------------------------------------------------------------------


def test_the_gateway_takes_a_context_and_refuses_a_bad_one(tmp_path):
    from unimem import gateway
    from tests.test_gateway import ITEMS, TOKEN, _start_experiment, recall

    root = str(tmp_path)
    _start_experiment(root)
    gw = gateway.Gateway(root, token=TOKEN)
    status, body = recall(gw, "occ-1", ITEMS, context="Billing")
    assert status == 200, body
    assert {r.context for r in holdout_io.read_log(root)[0]} == {"billing"}
    status, body = recall(gw, "occ-2", ITEMS, context="x" * 65)
    assert status == 400 and "context" in json.dumps(body)


def test_mcp_retrieve_logs_the_agent_type_and_the_report_tool_answers(tmp_path):
    pytest.importorskip("mcp", reason="needs the MCP SDK")
    from unimem import mcp_server
    from tests.test_mcp_server import _write_lesson, call, cli

    root = str(tmp_path / "store")
    assert cli("init", "--dest", root, "--agent-type", "support").returncode == 0
    _write_lesson(root, "verify-owner", description="password reset: verify the account owner",
                  body="Before sending a password reset email, verify the account owner.", agent_type="support")
    holdout_io.configure(root, rate=0.5, salt="pinned")
    server = mcp_server.build_server(root)
    out = call(server, "retrieve", task="password reset email", occasion_id="occ-1", agent_type="support")
    assert out.get("ok", True), out
    assert {r.context for r in holdout_io.read_log(root)[0]} == {"support"}
    report = call(server, "causal_gate_report")
    assert report["model"]["rows"] == 0 and "note" in report
    assert "error" in json.dumps(call(server, "causal_gate_report", floor=0.7)).lower()
