"""The coverage harness itself: its arithmetic and its CLI. The 200-seed evidence is
a slow opt-in run (commons/eval/CAUSAL_COVERAGE.md), not a unit test."""
from commons.eval import coverage_harness as ch


def _run(good, bad, neutral):
    def row(verdict, effect, truth):
        return {"verdict": verdict, "effect": effect, "ci_low": effect - 0.05, "ci_high": effect + 0.05}
    return {ch.GOOD: row(*good), ch.BAD: row(*bad), ch.NEUTRAL: row(*neutral)}


def test_summarise_scores_verdicts_and_interval_coverage_against_the_planted_truth():
    runs = [_run(("HELPS", 0.2, 0.2), ("HURTS", -0.2, -0.2), ("NO_MEASURABLE_EFFECT", 0.0, 0.0)) for _ in range(9)]
    runs.append(_run(("NO_MEASURABLE_EFFECT", 0.0, 0.2), ("HURTS", -0.2, -0.2), ("HELPS", 0.2, 0.0)))
    summary = ch.summarise("x", runs)
    assert summary["memories"][ch.GOOD]["verdict_correct"] == 0.9
    assert summary["memories"][ch.GOOD]["ci_coverage"] == 0.9      # the miss's interval excludes +0.20
    assert summary["memories"][ch.NEUTRAL]["verdict_correct"] == 0.9  # a false HELPS is wrong
    assert summary["memories"][ch.BAD]["verdict_correct"] == 1.0
    assert summary["passed"] is True
    assert ch.summarise("x", runs[:5] + [runs[-1]] * 5)["passed"] is False


def test_a_few_real_seeds_run_end_to_end_for_two_adapters(capsys):
    assert ch.main(["--scenario", "coverage", "--seeds", "2", "--adapters", "mem0,agentcore",
                    "--jobs", "1", "--occasions", "150", "--json"]) in (0, 1)  # 2 seeds prove nothing
    import json
    report = json.loads(capsys.readouterr().out)
    assert [c["adapter"] for c in report["coverage"]] == ["mem0", "agentcore"]
    assert set(report["coverage"][0]["memories"]) == {ch.GOOD, ch.BAD, ch.NEUTRAL}


def test_every_adapter_has_a_fake_and_an_unknown_one_is_refused(capsys):
    assert set(ch.ADAPTERS) == {"mem0", "letta", "zep", "claude-memory-store", "agentcore"}
    assert ch.main(["--adapters", "nope"]) == 2
    assert "unknown adapter" in capsys.readouterr().err
