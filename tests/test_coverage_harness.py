"""The coverage harness itself: its arithmetic and its CLI. The 200-seed evidence is
a slow opt-in run, not a unit test."""
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


def test_a_mid_run_edit_is_flagged_compromised_and_random_attrition_is_not_blamed_on_the_memory(capsys):
    edits = ch.edits(seeds=3, jobs=1, occasions=200)
    assert edits["passed"] and edits["edited_memory_flagged_compromised"] == 1.0
    runs = ch._run_variant("random_attrition", 2, 1, 300)
    assert all(r["lost"] > 0 for r in runs) and all(set(r["memories"]) >= {ch.GOOD} for r in runs)


def test_loss_that_follows_the_outcome_and_the_treatment_is_flagged_by_the_audit():
    runs = ch._run_variant("differential_attrition", 6, 1, 400)
    assert sum(r["audit"] != "SOUND" for r in runs) >= 5   # flagged, not quietly quoted
