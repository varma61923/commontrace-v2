"""The Agent Learning Proof: plan, run, report, and -- the point -- verify from raw data."""
import csv
import json
import os
import random

import pytest

from commontrace import experiment, functions, holdout_io, proof, retrieval_io
from commontrace.cli import main

KEY = b"0123456789abcdef-test-signing-key"


@pytest.fixture(autouse=True)
def _no_fsync(monkeypatch):
    """Thousands of log lines per test; durability is covered in tests/test_holdout_io*.py."""
    monkeypatch.setattr(os, "fsync", lambda fd: None)

SUPPORT = functions.builtin_kits()["support"]


def _run_fleet(root, n, *, harm=-0.25, help_=+0.2, seed=1, rates=None):
    """A fleet whose memories are eligible on different occasions (so their effects can be
    added): `help` +20pp, `harm` -25pp, `null` nothing. Outcomes are drawn here."""
    config = holdout_io.load_config(root)
    rng = random.Random(seed)
    truth = {"helps": help_, "hurts": harm, "null": 0.0}
    slugs = list(truth)
    for i in range(n):
        slug = slugs[i % 3]
        rate = (rates or {}).get(i, config.rate)
        withheld = holdout_io.assign_and_log(
            root, [slug], occasion_id=f"T-{i}", rate=rate, salt=config.salt, revisions={slug: "r1"})
        p = 0.6 + (truth[slug] if slug not in withheld else 0.0)
        holdout_io.record_outcome(root, f"T-{i}", rng.random() < p)


@pytest.fixture
def started(tmp_path, monkeypatch):
    # A pinned salt: the fleets below draw outcomes from a seeded rng, and a fresh salt per run would re-deal the
    # arms, so the null memory would show a chance effect in about one run in twenty.
    real = holdout_io.configure
    monkeypatch.setattr(holdout_io, "configure", lambda *a, **kw: real(*a, **{**kw, "salt": kw.get("salt") or "pinned"}))
    root = str(tmp_path / "store")
    state, _ = proof.start(root, SUPPORT, label="Acme support", daily=300, value_per_occasion=12.0)
    return root, state


def _package(root, tmp_path, key=KEY):
    out = str(tmp_path / "pkg")
    record = proof.build(root, out, key=key)
    return out, record


def _verify(out, key=KEY):
    return {c.name: c for c in proof.verify(out, key=key)}


# --- Start ------------------------------------------------------------------------


def test_start_registers_the_design_before_any_data_and_starts_the_holdout(started):
    root, state = started
    reg = state["preregistration"]
    assert reg["primary_outcome"] == SUPPORT.outcome.success
    assert reg["stopping_rule"] == "sequential" and reg["planned_occasions"] == 2638
    config = holdout_io.load_config(root)
    assert config.running and config.salt == reg["salt"]
    assert not os.path.exists(holdout_io.holdout_log_path(root))
    assert proof.load_state(root)["label"] == "Acme support"


def test_a_run_that_cannot_answer_in_time_is_refused_unless_forced(tmp_path):
    root = str(tmp_path / "s")
    with pytest.raises(proof.ProofError, match="spent pilot") as exc:
        proof.start(root, SUPPORT, label="x", daily=5)
    assert "too low to answer" in str(exc.value)
    assert proof.load_state(root) is None
    state, _ = proof.start(root, SUPPORT, label="x", daily=5, force=True)
    assert state["forecast_days_to_verdict"] > proof.MAX_DAYS


def test_a_proof_in_progress_is_not_silently_replaced(started):
    root, _ = started
    with pytest.raises(proof.ProofError, match="already has a proof"):
        proof.start(root, SUPPORT, label="again", daily=300)
    proof.start(root, SUPPORT, label="again", daily=300, force=True)
    assert proof.load_state(root)["label"] == "again"


@pytest.mark.parametrize("kwargs, message", [
    ({"label": " "}, "label is required"), ({"value_per_occasion": 0}, "positive"),
    ({"value_per_occasion": -3}, "positive"), ({"daily": 0}, "daily_occasions"),
    ({"baseline": 1.5}, "baseline"),
])
def test_bad_inputs_are_refused_and_start_nothing(tmp_path, kwargs, message):
    root = str(tmp_path / "s")
    args = {"label": "x", "daily": 300, **kwargs}
    with pytest.raises(proof.ProofError, match=message):
        proof.start(root, SUPPORT, **args)
    assert proof.load_state(root) is None


# --- Status -----------------------------------------------------------------------


def test_status_walks_from_no_data_to_collecting_to_ready(started):
    root, _ = started
    assert proof.status(root).state == "no-data"
    _run_fleet(root, 150)
    mid = proof.status(root)
    assert mid.state == "collecting" and 0 < mid.progress < 1 and mid.resolved_occasions == 150
    assert mid.integrity and "not yet decided" in mid.next_step
    done = _fleet_to(root, 2700)
    assert done.state == "ready" and done.progress == 1.0


def _fleet_to(root, n):
    config = holdout_io.load_config(root)
    rng = random.Random(5)
    truth = {"helps": 0.2, "hurts": -0.25, "null": 0.0}
    slugs = list(truth)
    have = len({r.occasion_id for r in holdout_io.read_log(root)[0]})
    for i in range(have, n):
        slug = slugs[i % 3]
        withheld = holdout_io.assign_and_log(root, [slug], occasion_id=f"T-{i}", rate=config.rate,
                                             salt=config.salt, revisions={slug: "r1"})
        holdout_io.record_outcome(root, f"T-{i}", rng.random() < 0.6 + (truth[slug] if slug not in withheld else 0.0))
    return proof.status(root)


def test_status_of_a_compromised_run_says_so_and_states_no_effect_to_trust(started):
    root, _ = started
    _run_fleet(root, 600, rates={i: 0.1 for i in range(200, 600)})  # two rates under one salt
    s = proof.status(root)
    assert s.state == "compromised" and s.integrity == "COMPROMISED"
    assert "cannot be trusted" in s.next_step


# --- Report and verify -------------------------------------------------------------


def _finished(root, n=2700):
    _fleet_to(root, n)


def test_a_signed_package_verifies_in_every_respect(started, tmp_path):
    root, _ = started
    _finished(root)
    out, record = _package(root, tmp_path)
    assert record["final"] and record["signature"]
    assert {m["lesson_slug"]: m["verdict"] for m in record["memories"]} == {
        "helps": "HELPS", "hurts": "HURTS", "null": "NO_MEASURABLE_EFFECT"}
    assert record["harmful"]["memories"] == ["hurts"] and not record["harmful"]["withdrawn_automatically"]
    assert record["ledger"] and record["value"]["money"] is not None
    checks = _verify(out)
    assert {c.status for c in checks.values()} == {proof.PASS}, [str(c) for c in checks.values()]
    assert set(checks) >= {"data digest", "recomputed estimates", "recomputed value ledger",
                           "ledger chain", "pre-registration", "issuer signature"}
    report = open(os.path.join(out, "report.md"), encoding="utf-8").read()
    assert report.index("## Result") < report.index("## What each memory did")
    assert "still being delivered" in report and "What this does not show" in report
    assert "SYNTHETIC" not in report


def test_the_report_leads_with_validity_before_effects(started, tmp_path):
    root, _ = started
    _finished(root)
    out, _ = _package(root, tmp_path)
    report = open(os.path.join(out, "report.md"), encoding="utf-8").read()
    assert report.index("Can this be trusted?") < report.index("| Memory | Verdict")


def test_an_interim_package_says_so(started, tmp_path):
    root, _ = started
    _run_fleet(root, 120)
    out, record = _package(root, tmp_path)
    assert not record["final"]
    assert "Interim" in open(os.path.join(out, "report.md"), encoding="utf-8").read()
    assert all(c.status != proof.FAIL for c in proof.verify(out, key=KEY))


def test_a_compromised_run_states_no_figure_and_the_refusal_itself_verifies(started, tmp_path):
    root, _ = started
    _run_fleet(root, 900, rates={i: 0.1 for i in range(300, 900)})
    out, record = _package(root, tmp_path)
    assert record["integrity"]["verdict"] == "COMPROMISED" and not record["value"]["readable"]
    assert record["ledger"] == [] and record["value"]["money"] is None
    assert "No figure is stated" in open(os.path.join(out, "report.md"), encoding="utf-8").read()
    assert proof.verified(proof.verify(out, key=KEY))


def test_an_unsigned_package_verifies_but_says_who_issued_it_is_not_established(started, tmp_path):
    root, _ = started
    _finished(root)
    out, record = _package(root, tmp_path, key=None)
    assert record["signature"] is None
    checks = _verify(out, key=None)
    assert checks["issuer signature"].status == proof.UNSIGNED and proof.verified(list(checks.values()))
    assert "Not signed" in open(os.path.join(out, "report.md"), encoding="utf-8").read()


def test_a_signed_package_without_the_key_skips_the_signature_check_and_says_so(started, tmp_path):
    root, _ = started
    _finished(root)
    out, _ = _package(root, tmp_path)
    assert _verify(out, key=None)["issuer signature"].status == proof.SKIP


def test_a_withdrawing_store_reports_the_harm_as_handled(started, tmp_path):
    root, _ = started
    cfg = retrieval_io.config_path(root)
    os.makedirs(os.path.dirname(cfg), exist_ok=True)
    json.dump({"harm_policy": "withdraw"}, open(cfg, "w"))
    _finished(root)
    out, record = _package(root, tmp_path)
    assert record["harmful"]["withdrawn_automatically"]
    assert "no longer delivered" in open(os.path.join(out, "report.md"), encoding="utf-8").read()


# --- Tampering: every one of these must be caught ----------------------------------


def _edit_json(out, fn):
    path = os.path.join(out, "proof.json")
    data = json.load(open(path, encoding="utf-8"))
    fn(data)
    json.dump(data, open(path, "w", encoding="utf-8"), indent=2, sort_keys=True)


@pytest.fixture
def package(started, tmp_path):
    root, _ = started
    _finished(root)
    out, record = _package(root, tmp_path)
    return out, record


def _failed(out, key=KEY):
    return {c.name for c in proof.verify(out, key=key) if c.status == proof.FAIL}


def test_flipping_one_outcome_in_the_raw_data_is_caught_twice(package):
    out, _ = package
    path = os.path.join(out, "assignments.csv")
    rows = list(csv.reader(open(path, encoding="utf-8")))
    for r in rows[1:]:
        if r[0] == "hurts" and r[2] == "injected" and r[3] == "false":
            r[3] = "true"
            break
    csv.writer(open(path, "w", encoding="utf-8", newline=""), lineterminator="\n").writerows(rows)
    assert {"data digest", "recomputed estimates"} <= _failed(out)


def test_dropping_the_inconvenient_rows_is_caught(package):
    out, _ = package
    path = os.path.join(out, "assignments.csv")
    rows = list(csv.reader(open(path, encoding="utf-8")))
    kept = [rows[0]] + [r for r in rows[1:] if not (r[0] == "hurts" and r[3] == "false")]
    csv.writer(open(path, "w", encoding="utf-8", newline=""), lineterminator="\n").writerows(kept)
    assert "data digest" in _failed(out)


def test_changing_a_verdict_in_the_report_is_caught_by_recomputation(package):
    out, _ = package
    _edit_json(out, lambda d: [m.update(verdict="HELPS") for m in d["memories"] if m["lesson_slug"] == "hurts"])
    assert "recomputed estimates" in _failed(out)


def test_inflating_the_money_on_a_ledger_line_is_caught(package):
    out, _ = package
    _edit_json(out, lambda d: d["ledger"][0].update(money=d["ledger"][0]["money"] * 10,
                                                    occasions_improved=d["ledger"][0]["occasions_improved"] * 10))
    failed = _failed(out)
    assert {"ledger chain", "recomputed value ledger"} <= failed


def test_replacing_the_whole_ledger_with_a_consistent_forgery_is_caught_by_the_signature_or_the_data(package):
    out, _ = package
    from commontrace import value

    def forge(d):
        entries, previous = [], value._LEDGER_GENESIS
        import hashlib
        for i, e in enumerate(d["ledger"]):
            occ = e["occasions_improved"] * 0.1
            row = value._FIELD_SEP.join((str(i), e["slug"], e["verdict"], f"{occ:.6f}", f"{e['rate']:.6f}",
                                         f"{occ * e['rate']:.6f}"))
            h = hashlib.sha256((previous + value._FIELD_SEP + row).encode()).hexdigest()
            entries.append({**e, "occasions_improved": occ, "money": round(occ * e["rate"], 2),
                            "previous_hash": previous, "entry_hash": h})
            previous = h
        d["ledger"], d["ledger_root"] = entries, previous
    _edit_json(out, forge)
    failed = _failed(out)
    assert "ledger chain" not in failed          # internally consistent, as a forger would make it
    assert {"issuer signature", "recomputed value ledger"} <= failed


def test_the_wrong_key_does_not_verify(package):
    out, _ = package
    assert "issuer signature" in _failed(out, key=b"a-completely-different-secret-key")


def test_swapping_the_registered_design_is_caught(package):
    out, _ = package
    _edit_json(out, lambda d: d["preregistration"].update(minimum_practical_effect=0.01))
    assert "pre-registration" in _failed(out)


def test_changing_the_digest_to_match_edited_data_breaks_the_signature(package):
    out, _ = package
    from commontrace import raw_export
    path = os.path.join(out, "assignments.csv")
    rows = list(csv.reader(open(path, encoding="utf-8")))
    rows[3][3] = "true" if rows[3][3] == "false" else "false"
    csv.writer(open(path, "w", encoding="utf-8", newline=""), lineterminator="\n").writerows(rows)
    text = open(path, encoding="utf-8").read()
    new_digest = raw_export.digest_of(proof.rows_from_csv(text))
    _edit_json(out, lambda d: d["evidence"].update(digest=new_digest))
    failed = _failed(out)
    assert "data digest" not in failed and "issuer signature" in failed


def test_a_signature_for_one_org_does_not_verify_as_another(package):
    out, _ = package
    _edit_json(out, lambda d: d.update(org_id="Some Other Customer"))
    assert "issuer signature" in _failed(out)


# --- Demo ---------------------------------------------------------------------------


def test_a_demo_proof_is_labelled_synthetic_everywhere_and_still_verifies(tmp_path):
    root = str(tmp_path / "demo")
    state = proof.start_demo(root, SUPPORT, value_per_occasion=12.0)
    assert state["synthetic"]
    out, record = _package(root, tmp_path)
    assert record["synthetic"] and record["preregistration_clean"]
    assert record["ledger"] and record["integrity"]["verdict"] == "SOUND"
    verdicts = {m["lesson_slug"]: m["verdict"] for m in record["memories"]}
    assert verdicts == {"demo-helpful-memory": "HELPS", "demo-harmful-memory": "HURTS",
                        "demo-neutral-memory": "NO_MEASURABLE_EFFECT"}
    assert "SYNTHETIC DEMO DATA" in open(os.path.join(out, "report.md"), encoding="utf-8").read()
    assert "SYNTHETIC DEMO DATA" in proof.render_status(proof.status(root))
    assert proof.verified(proof.verify(out, key=KEY))


def test_a_demo_never_goes_into_a_store_with_real_data(started):
    root, _ = started
    _run_fleet(root, 30)
    with pytest.raises(functions.KitError, match="real holdout data"):
        proof.start_demo(root, SUPPORT)


# --- Inputs and the CLI ----------------------------------------------------------------


def test_keys_must_be_long_enough_and_readable(tmp_path):
    short = tmp_path / "short"
    short.write_bytes(b"abc")
    with pytest.raises(proof.ProofError, match="can be guessed"):
        proof.read_key(str(short))
    with pytest.raises(proof.ProofError, match="cannot read"):
        proof.read_key(str(tmp_path / "missing"))
    ok = tmp_path / "ok"
    ok.write_bytes(KEY + b"\n")
    assert proof.read_key(str(ok)) == KEY


def test_verifying_something_that_is_not_a_package_is_an_error_not_a_pass(tmp_path):
    with pytest.raises(proof.ProofError, match="cannot read the package"):
        proof.verify(str(tmp_path))


def test_a_malformed_csv_fails_recomputation_rather_than_crashing(package):
    out, _ = package
    open(os.path.join(out, "assignments.csv"), "w", encoding="utf-8").write("not,the,columns\n1,2,3\n")
    assert {"data digest", "recomputation"} <= _failed(out)


def test_the_cli_runs_the_whole_flow(tmp_path, capsys):
    root = str(tmp_path / "store")
    key = tmp_path / "key"
    key.write_bytes(KEY)
    assert main(["init", "--function", "support", "--dest", root]) == 0
    assert main(["proof", "start", "--label", "Acme", "--daily", "300", "--value-per-occasion", "12",
                 "--dest", root]) == 0
    assert "registered before any data" in capsys.readouterr().out.lower()
    assert main(["proof", "start", "support", "--label", "Acme", "--daily", "5",
                 "--dest", str(tmp_path / "x")]) == 2
    _finished(root)
    assert main(["proof", "status", "--dest", root]) == 0
    out = str(tmp_path / "pkg")
    assert main(["proof", "report", "--out", out, "--key-file", str(key), "--dest", root]) == 0
    capsys.readouterr()
    assert main(["proof", "verify", out, "--key-file", str(key)]) == 0
    assert "verified" in capsys.readouterr().out
    _edit_json(out, lambda d: [m.update(verdict="HELPS") for m in d["memories"]])
    assert main(["proof", "verify", out, "--key-file", str(key)]) == 1
    assert "NOT VERIFIED" in capsys.readouterr().out
    assert main(["proof", "verify", str(tmp_path / "nope")]) == 2


def test_the_cli_demo_then_report_then_verify(tmp_path, capsys):
    root = str(tmp_path / "demo")
    assert main(["init", "--function", "sales", "--dest", root]) == 0
    assert main(["proof", "demo", "--value-per-occasion", "40", "--dest", root]) == 0
    out = str(tmp_path / "pkg")
    assert main(["proof", "report", "--out", out, "--dest", root]) == 0
    assert "SYNTHETIC DEMO DATA" in capsys.readouterr().out
    assert main(["proof", "verify", out]) == 0


def test_status_json_is_machine_readable(started, capsys):
    root, _ = started
    _run_fleet(root, 60)
    assert main(["proof", "status", "--json", "--dest", root]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["state"] == "collecting" and data["resolved_occasions"] == 60


def test_the_planned_effect_is_what_the_forecast_says():
    assert experiment.plan(effect=0.05, baseline=0.70, rate=0.5).occasions_needed == 2638


# --- What withdrawing the harmful memory would give back --------------------------------


def test_the_report_says_what_stopping_the_harmful_memory_gives_back(started, tmp_path):
    root, _ = started
    _finished(root)
    out, record = _package(root, tmp_path)
    rec = record["harmful"]["recoverable"]
    assert rec["memories"] == ["hurts"] and rec["occasions"] > 0 and rec["ci_95"][0] < rec["occasions"] < rec["ci_95"][1]
    assert rec["money"] == pytest.approx(rec["occasions"] * 12.0)
    text = open(os.path.join(out, "report.md"), encoding="utf-8").read()
    assert "That is what stopping them gives back" in text and "not billed" in text


def test_inflating_the_recoverable_figure_is_caught(package):
    out, _ = package
    _edit_json(out, lambda d: d["harmful"]["recoverable"].update(occasions=d["harmful"]["recoverable"]["occasions"] * 5))
    assert "recomputed harm recovery" in _failed(out)


def test_no_recovery_is_stated_when_nothing_hurts_or_the_run_is_unreadable(started, tmp_path):
    root, _ = started
    _run_fleet(root, 900, rates={i: 0.1 for i in range(300, 900)})  # compromised: no figure at all
    _, record = _package(root, tmp_path)
    assert record["harmful"]["recoverable"] is None
